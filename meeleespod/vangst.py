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
            if _venster.get("dienst"):
                _venster.setdefault("procs", []).append(proc)
                al_dicht = False
            else:
                al_dicht = True   # het venster is net gesloten (knop/timer) in het gaatje na Popen: deze vanger niet verweesd laten draaien
        if al_dicht:
            _stop(proc)
            log("vanger_al_dicht", dienst=dienst, modus=modus)
            return 5, {"ok": False, "code": "GESTOPT"}
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


# ---- profiel opschonen (review MOET 6) -------------------------------------------------------------------------------
# Teams-web en OWA bewaren berichten in IndexedDB, Cache Storage en service workers (de pc-scripts lazen juist die
# databank). Na elke vangst of Parro-ronde gaat dat weg; cookies en Local Storage (de MSAL-sessie) blijven, anders
# kan er niet stil vernieuwd worden.
WIS_IN_PROFIEL = ("IndexedDB", "Service Worker", "Cache", "Code Cache", "GPUCache", "File System", "blob_storage",
                  "Session Storage", "Sessions", "shared_proto_db", "WebStorage", "DawnWebGPUCache", "DawnGraphiteCache")


def wis_berichtcache(dienst):
    import shutil
    prof = I.GEHEIM / dienst / "profiel"
    n = 0
    for basis in [prof] + [p for p in prof.glob("*") if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile"))]:
        for naam in WIS_IN_PROFIEL:
            doel = basis / naam
            if doel.is_symlink():
                doel.unlink()
                n += 1
            elif doel.exists():
                shutil.rmtree(doel, ignore_errors=True)
                n += 1
    log("profiel_opgeschoond", dienst=dienst, aantal=n)


# ---- het zichtbare inlogvenster --------------------------------------------------------------------------------------
# Review MOET 1: wie vanuit het clusternetwerk de pagina-poort bereikt, kan Origin en Host zelf kiezen. Daarom opent een
# klik NOOIT meteen een venster: eerst stil vernieuwen (geen VNC). Lukt dat, dan is er niets te tonen. Pas als echt
# ingelogd moet worden, start het venster — en dan staat er een inlogpagina van Microsoft of Parro in, geen postvak.
# De eenmalige VNC-code gaat alleen naar wie de klik deed (aanvraagtoken), nooit naar /stand.json.

def _vrije_poort():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


_aanvragen = {}   # id -> {"t_hash", "fase", "code", "dienst", "tijd"}


def venster_stand():
    with _venster_slot:
        if not _venster.get("dienst"):
            return None
        return {"dienst": _venster["dienst"], "sinds": _venster["sinds"]}


def aanvraag_stand(aid, token):
    """Voor de pagina van de aanvrager: fase controleren | klaar | venster | mislukt, en bij venster de code (één keer)."""
    with _venster_slot:
        a = _aanvragen.get(aid)
        if not a or not token or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), a["t_hash"]):
            return None
        uit = {"fase": a["fase"], "dienst": a["dienst"]}
        if a["fase"] == "venster" and a.get("code"):
            uit["code"] = a.pop("code")
        return uit


def start_inlogvenster(dienst, na_vangst):
    """Begint een inlogpoging voor `dienst` op de achtergrond. Geeft {"id", "token"} of {"fout"}."""
    if dienst not in I.DIENSTEN:
        return {"fout": "onbekende dienst"}
    if not browser_slot.acquire(timeout=2):
        return {"fout": "er loopt al een browser (ronde of inlogvenster); probeer het over een minuut opnieuw"}
    aid, token = secrets.token_urlsafe(12), secrets.token_urlsafe(24)
    with _venster_slot:
        for k in [k for k, v in _aanvragen.items() if time.time() - v["tijd"] > 3600]:
            _aanvragen.pop(k, None)
        _aanvragen[aid] = {"t_hash": hashlib.sha256(token.encode()).hexdigest(), "fase": "controleren",
                           "dienst": dienst, "tijd": time.time()}

    def fase(f, **extra):
        with _venster_slot:
            if aid in _aanvragen:
                _aanvragen[aid].update(fase=f, **extra)

    def loop():
        code_, uitslag = 1, {"ok": False, "code": "FOUT"}
        try:
            # 1. stil (zonder VNC): al ingelogd? dan is er geen venster nodig
            if dienst == "parro":
                from . import parro
                code_, uitslag = parro.inloggen_stil()
            else:
                code_, uitslag = draai_vanger(dienst, "stil", 300)
            wis_berichtcache(dienst)
            if code_ == 0 and uitslag.get("ok"):
                fase("klaar")
                return
            # 2. echt inloggen: venster met eenmalige code
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
                _venster.update(id=aid, dienst=dienst, code_hash=hashlib.sha256(code.encode()).hexdigest(),
                                sinds=datetime.now(timezone.utc).isoformat(), aangemaakt=time.time(), poort=poort,
                                procs=[vnc], gebruikt=False)
            fase("venster", code=code)
            tijd = datetime.now().strftime("%H:%M")
            staat.werk_bij("inlogvensters", lambda d: d.__setitem__(
                "starts", (d.get("starts") or [])[-19:] + [{"dienst": dienst, "tijd": datetime.now(timezone.utc).isoformat()}]))
            threading.Thread(target=lambda: webhook.alarm(dienst, "inlogvenster", f"inlogvenster {dienst} geopend om {tijd}"),
                             daemon=True).start()
            log("inlogvenster_open", dienst=dienst)
            bewaker = threading.Timer(I.INLOGVENSTER_MAX_S + 60, sluit_inlogvenster, kwargs={"alleen_id": aid})
            bewaker.daemon = True
            bewaker.start()
            if dienst == "parro":
                from . import parro
                code_, uitslag = parro.inloggen_zichtbaar(I.INLOGVENSTER_MAX_S)
            else:
                code_, uitslag = draai_vanger(dienst, "zichtbaar", I.INLOGVENSTER_MAX_S + 30)
            bewaker.cancel()
            wis_berichtcache(dienst)
            fase("klaar" if code_ == 0 and uitslag.get("ok") else "mislukt")
        except Exception as e:
            log("inlogvenster_fout", dienst=dienst, code=type(e).__name__)
            fase("mislukt")
        finally:
            sluit_inlogvenster(alleen_id=aid)
            browser_slot.release()
            try:
                na_vangst(dienst, code_, uitslag)
            except Exception as e:
                log("na_vangst_fout", dienst=dienst, code=type(e).__name__)

    try:
        threading.Thread(target=loop, daemon=True).start()
    except Exception:
        browser_slot.release()
        raise
    return {"id": aid, "token": token}


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


def geef_vnc_code_terug(code):
    """Verbinding met x11vnc mislukte: de code mag nog één keer (zelfde 2 minuten)."""
    h = hashlib.sha256((code or "").encode()).hexdigest()
    with _venster_slot:
        if _venster.get("code_hash") and secrets.compare_digest(h, _venster["code_hash"]):
            _venster["gebruikt"] = False


def sluit_inlogvenster(alleen_id=None):
    """Sluit het venster (en de processen ervan). alleen_id: alleen als dát venster nog open is (review: een oude timer
    mag geen nieuw venster dichtdoen)."""
    with _venster_slot:
        if alleen_id is not None and _venster.get("id") not in (None, alleen_id):
            return
        procs = list(_venster.get("procs", []))
        dienst = _venster.get("dienst")
        _venster.clear()
    for p in procs:
        _stop(p)
    if dienst:
        log("inlogvenster_dicht", dienst=dienst)


def vnc_draait():
    """Voor de toets: draait er een x11vnc?"""
    with _venster_slot:
        return any(p.poll() is None for p in _venster.get("procs", []) if p.args and p.args[0] == "x11vnc")
