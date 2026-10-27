"""Inloggen en vernieuwen: virtueel scherm, het ene browserslot, de Node-vanger en het zichtbare inlogvenster.

- Eén browser tegelijk (slot): Parro-ronde, stil vernieuwen en het zichtbare inlogvenster wachten op elkaar.
- Zichtbaar venster (ontwerp § 6): alleen na een klik op de inlogpagina. Dan pas start x11vnc, alleen op localhost, op
  een willekeurige poort, met -once (stopt na de eerste verbinding) en een eenmalige code die de pagina bij die klik
  krijgt. Het venster sluit zodra het token gevangen is, of na 10 minuten. Elke start gaat als alarm-regel naar n8n.
"""
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import instellingen as I
from . import staat, webhook
from .logboek import filter_stderr, log

browser_slot = threading.Lock()
_scherm = {"proc": None}
_scherm_slot = threading.Lock()
_venster = {}            # het ene zichtbare inlogvenster: dienst, code_hash, sinds, poort, procs, gebruikt
_venster_slot = threading.Lock()


def thuis():
    p = Path(os.environ.get("HOME") or "/tmp/thuis")
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def dienstmap(dienst):
    p = I.GEHEIM / dienst
    for sub in ("", "home", "profiel"):
        (p / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(p / sub, 0o700)
    return p


def _xauthority():
    return thuis() / ".Xauthority"


def zorg_voor_scherm():
    """Start Xvfb (alleen unix-socket, met een eigen cookie) als hij niet draait."""
    with _scherm_slot:
        p = _scherm["proc"]
        if p is not None and p.poll() is None:
            return
        xa = _xauthority()
        cookie = secrets.token_hex(16)
        xa.touch(mode=0o600)
        subprocess.run(["xauth", "-f", str(xa), "add", I.DISPLAY, ".", cookie], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _scherm["proc"] = subprocess.Popen(
            ["Xvfb", I.DISPLAY, "-screen", "0", I.SCHERM, "-nolisten", "tcp", "-auth", str(xa)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        nummer = I.DISPLAY.lstrip(":").split(".")[0]
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{nummer}").exists():
                break
            time.sleep(0.1)
        log("scherm_gestart", status="ok")


def kind_omgeving(dienst):
    d = dienstmap(dienst)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(d / "home"),
        "MEELEES_PROFIEL": str(d / "profiel"),
        "DISPLAY": I.DISPLAY,
        "XAUTHORITY": str(_xauthority()),
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"),
        "TZ": os.environ.get("TZ", "Europe/Amsterdam"),
        "LANG": "nl_NL.UTF-8",
        "TMPDIR": "/tmp",
    }


def _stop(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def draai_vanger(dienst, modus, timeout_s):
    """Draait de Node-vanger. Geeft (code, uitslag-dict). uitslag bevat nooit een token."""
    zorg_voor_scherm()
    uitmap = dienstmap(dienst)
    proc = subprocess.Popen(["node", I.VANGER, dienst, modus, str(uitmap)], env=kind_omgeving(dienst),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    if modus == "zichtbaar":
        with _venster_slot:
            _venster.setdefault("procs", []).append(proc)
    try:
        uit, fout = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(proc)
        uit, fout = b"", b""
        log("vanger_tijd_op", dienst=dienst, modus=modus)
        return 5, {"ok": False, "code": "TIJD_OP"}
    goed, weg = filter_stderr(fout.decode("utf-8", "replace"))
    uitslag = {}
    for r in reversed(uit.decode("utf-8", "replace").splitlines()):
        try:
            uitslag = json.loads(r)
            break
        except ValueError:
            continue
    uitslag = {k: uitslag[k] for k in ("ok", "code", "geldig_tot", "audiences", "graph_mail_read") if k in uitslag}
    log("vanger", dienst=dienst, modus=modus, exit=proc.returncode, code=str(uitslag.get("code", "-")),
        overig=weg, status=(goed[-1] if goed else "-"))
    return proc.returncode, uitslag


# ---- het zichtbare inlogvenster --------------------------------------------------------------------------------------

def _vrije_poort():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def venster_stand():
    with _venster_slot:
        if not _venster.get("dienst"):
            return None
        return {"dienst": _venster["dienst"], "sinds": _venster["sinds"]}


def start_inlogvenster(dienst, na_vangst):
    """Start het zichtbare venster voor `dienst`. Geeft {"code": ...} of {"fout": ...}.
    na_vangst(dienst, exitcode, uitslag) wordt aangeroepen als het venster dicht is."""
    if dienst not in I.DIENSTEN:
        return {"fout": "onbekende dienst"}
    if not browser_slot.acquire(timeout=2):
        return {"fout": "er loopt al een browser (ronde of inlogvenster); probeer het over een minuut opnieuw"}
    try:
        zorg_voor_scherm()
        poort = _vrije_poort()
        vnc = subprocess.Popen(
            ["x11vnc", "-display", I.DISPLAY, "-auth", str(_xauthority()), "-localhost", "-rfbport", str(poort),
             "-once", "-timeout", str(I.VNC_CODE_GELDIG_S), "-nopw", "-noxdamage", "-quiet", "-nossl", "-norc",
             "-nocmds", "-noremote", "-no6"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        code = secrets.token_urlsafe(24)
        with _venster_slot:
            _venster.clear()
            _venster.update(dienst=dienst, code_hash=hashlib.sha256(code.encode()).hexdigest(),
                            sinds=datetime.now(timezone.utc).isoformat(), aangemaakt=time.time(), poort=poort,
                            procs=[vnc], gebruikt=False)
    except Exception as e:
        browser_slot.release()
        log("inlogvenster_mislukt", dienst=dienst, code=type(e).__name__)
        return {"fout": "het inlogvenster kon niet starten"}

    tijd = datetime.now().strftime("%H:%M")
    staat.werk_bij("inlogvensters", lambda d: d.setdefault("starts", []).append(
        {"dienst": dienst, "tijd": datetime.now(timezone.utc).isoformat()}) or d.__setitem__("starts", d["starts"][-20:]))
    threading.Thread(target=lambda: webhook.alarm(dienst, "inlogvenster",
                                                  f"inlogvenster {dienst} geopend om {tijd}"), daemon=True).start()
    log("inlogvenster_open", dienst=dienst)

    def loop():
        code_, uitslag = 1, {"ok": False, "code": "FOUT"}
        try:
            if dienst == "parro":
                from . import parro
                code_, uitslag = parro.inloggen_zichtbaar(I.INLOGVENSTER_MAX_S)
            else:
                code_, uitslag = draai_vanger(dienst, "zichtbaar", I.INLOGVENSTER_MAX_S + 30)
        except Exception as e:
            log("inlogvenster_fout", dienst=dienst, code=type(e).__name__)
        finally:
            sluit_inlogvenster()
            browser_slot.release()
        try:
            na_vangst(dienst, code_, uitslag)
        except Exception as e:
            log("na_vangst_fout", dienst=dienst, code=type(e).__name__)

    threading.Thread(target=loop, daemon=True).start()
    threading.Timer(I.INLOGVENSTER_MAX_S + 60, sluit_inlogvenster).start()   # vangnet bovenop de 10 min van de vanger
    return {"code": code}


def neem_vnc_code(code):
    """Eenmalig: geeft de VNC-poort als de code klopt, nog niet gebruikt is en niet verlopen. Anders None."""
    if not code:
        return None
    h = hashlib.sha256(code.encode()).hexdigest()
    with _venster_slot:
        if not _venster.get("code_hash") or not secrets.compare_digest(h, _venster["code_hash"]):
            return None
        if _venster.get("gebruikt") or time.time() - _venster.get("aangemaakt", 0) > I.VNC_CODE_GELDIG_S:
            return None
        _venster["gebruikt"] = True
        return _venster["poort"]


def sluit_inlogvenster():
    with _venster_slot:
        procs = list(_venster.get("procs", []))
        dienst = _venster.get("dienst")
        _venster.clear()
    for p in procs:
        _stop(p)
    if dienst:
        log("inlogvenster_dicht", dienst=dienst)


def vnc_draait():
    """Voor de rooktest: draait er een x11vnc?"""
    with _venster_slot:
        return any(p.poll() is None for p in _venster.get("procs", []) if p.args and p.args[0] == "x11vnc")
