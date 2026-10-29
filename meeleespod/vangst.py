"""Inloggen en vernieuwen: virtueel scherm, het ene browserslot, de Node-vanger en het zichtbare inlogvenster.

- Eén browser tegelijk (slot): Parro-ronde, stil vernieuwen en het zichtbare inlogvenster wachten op elkaar.
- Zichtbaar venster (ontwerp § 6): alleen na een klik op de inlogpagina. Dan pas start x11vnc, alleen op localhost, op
  een willekeurige poort, met -once (stopt na de eerste verbinding) en een eenmalige code die de pagina bij die klik
  krijgt. Het venster sluit zodra het token gevangen is, of na 10 minuten. Elke start gaat als alarm-regel naar n8n.
"""
import hashlib
import json
import os
import re
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


# ---- profielslot (wv1950) --------------------------------------------------------------------------------------------
# Chromium zet in het profiel SingletonLock (symlink "<hostnaam>-<pid>"), SingletonSocket en SingletonCookie. Gemeten
# 29-10-2026: Outlook gaf sinds 28-10 ± 21:05Z bij elke start BROWSERSTART, Parro (ander profiel) startte wel.
# Twee wegen naar een vastzittend slot: (1) Playwright start Chromium `detached` (eigen procesgroep; playwright-core
# processLauncher), dus `_stop` (killpg van de vanger) raakt Chromium niet: na een time-out kan een wees-Chromium het
# profiel vasthouden; (2) na een podherstart wijst het slot naar de hostnaam van de vorige pod en weigert Chromium het
# profiel ("in use on another computer"). Elke browserstart gebeurt met `browser_slot` vast (één browser tegelijk), dus
# een Chromium die dan nog dit profiel open heeft, is per definitie een wees. Het profiel zelf (cookies, Local Storage,
# Parro-IndexedDB) blijft ongemoeid.
SLOTBESTANDEN = ("SingletonLock", "SingletonSocket", "SingletonCookie")
# Subcode bij BROWSERSTART: vaste witte lijst, nooit fouttekst of paden (zelfde lijst in vanger/src/vanger.ts).
BROWSER_SUBCODES = ("SLOT_IN_GEBRUIK", "GEEN_BINARY", "GEEN_SCHERM", "GEHEUGEN", "SCHIJF_VOL", "STARTTIJD",
                    "DOEL_GESLOTEN", "ANDERS")


def browser_subcode(bericht):
    """Fouttekst van Playwright/Chromium → één subcode uit BROWSER_SUBCODES (volgorde telt: de slotmelding staat in
    de browserlogs ván een 'Target ... closed'-fout)."""
    b = bericht or ""
    if re.search(r"ProcessSingleton|profile appears to be in use|SingletonLock|existing browser session", b, re.I):
        return "SLOT_IN_GEBRUIK"
    if re.search(r"Executable doesn't exist|error while loading shared libraries|ENOENT|Cannot find module", b, re.I):
        return "GEEN_BINARY"
    if re.search(r"Missing X server|cannot open display|\$DISPLAY", b, re.I):
        return "GEEN_SCHERM"
    if re.search(r"ENOMEM|out of memory|Cannot allocate memory", b, re.I):
        return "GEHEUGEN"
    if re.search(r"ENOSPC|No space left", b, re.I):
        return "SCHIJF_VOL"
    if re.search(r"Timeout \d+ms exceeded", b, re.I):
        return "STARTTIJD"
    if re.search(r"Target page, context or browser has been closed|has been closed", b, re.I):
        return "DOEL_GESLOTEN"
    return "ANDERS"


class BrowserStartFout(Exception):
    """Python-Playwright (Parro) kon de browser niet starten; alleen de subcode gaat mee."""

    def __init__(self, sub):
        super().__init__(sub)
        self.sub = sub if sub in BROWSER_SUBCODES else "ANDERS"


def _profiel_pids(prof):
    doel = ("--user-data-dir=" + str(prof)).encode()
    pids = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            delen = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if doel in delen:
            pids.append(int(p.name))
    return pids


def ruim_profielslot(dienst):
    """Alleen aanroepen met `browser_slot` vast, vlak vóór een browserstart: wees-Chromium op dit profiel stoppen
    (eerst TERM, na 5 s KILL) en de drie slotbestanden weghalen. Geeft (wezen, bestanden)."""
    prof = I.GEHEIM / dienst / "profiel"
    wezen = _profiel_pids(prof)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in wezen:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        for _ in range(50):
            if not _profiel_pids(prof):
                break
            time.sleep(0.1)
        if not _profiel_pids(prof):
            break
    n = 0
    for naam in SLOTBESTANDEN:
        f = prof / naam
        try:
            if f.is_symlink() or f.exists():
                f.unlink()
                n += 1
        except OSError:
            pass
    if wezen or n:
        log("profielslot_opgeruimd", dienst=dienst, aantal=n, bekend=len(wezen))
    return len(wezen), n


def draai_vanger(dienst, modus, timeout_s):
    """Draait de Node-vanger. Geeft (code, uitslag-dict). uitslag bevat nooit een token."""
    zorg_voor_scherm()
    uitmap = dienstmap(dienst)
    ruim_profielslot(dienst)
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
        ruim_profielslot(dienst)   # Chromium draait detached: anders blijft hij als wees het profiel vasthouden
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
    uitslag = {k: uitslag[k] for k in ("ok", "code", "geldig_tot", "audiences", "graph_mail_read", "sub", "gestart")
               if k in uitslag}
    if "code" not in uitslag:
        # wv1950 (gemeten 29-10 09:56Z): de vanger kan sterven zonder JSON-regel (signaal, onafgevangen fout). Dan alleen
        # exitcode of signaal en een subcode uit de witte lijst op grond van stderr; nooit de tekst zelf.
        rc = proc.returncode if isinstance(proc.returncode, int) else -1
        uitslag["code"] = f"EXIT_{rc}" if rc >= 0 else f"SIGNAAL_{-rc}"
        uitslag["sub"] = browser_subcode(fout.decode("utf-8", "replace"))
    if "sub" in uitslag and uitslag["sub"] not in BROWSER_SUBCODES:
        uitslag["sub"] = "ANDERS"
    if "gestart" in uitslag:
        uitslag["gestart"] = bool(uitslag["gestart"])
    log("vanger", dienst=dienst, modus=modus, exit=proc.returncode, code=str(uitslag.get("code", "-")),
        soort=uitslag.get("sub", "-"), overig=weg, status=(goed[-1] if goed else "-"))
    return proc.returncode, uitslag


# ---- profiel opschonen (review MOET 6) -------------------------------------------------------------------------------
# Teams-web en OWA bewaren berichten in IndexedDB, Cache Storage en service workers (de pc-scripts lazen juist die
# databank). Na elke vangst of Parro-ronde gaat dat weg; cookies en Local Storage (de MSAL-sessie) blijven, anders
# kan er niet stil vernieuwd worden. Voor Parro blijft ook IndexedDB staan (zie BEHOUD_IN_PROFIEL).
WIS_IN_PROFIEL = ("IndexedDB", "Service Worker", "Cache", "Code Cache", "GPUCache", "File System", "blob_storage",
                  "Session Storage", "Sessions", "shared_proto_db", "WebStorage", "DawnWebGPUCache", "DawnGraphiteCache")
# Parro (wv1751, gemeten 28-10-2026): de Parro-webapp (Flutter) bewaart accessToken, refreshToken en expiresAt in een
# SQLite-databank `parro_db` (Drift), die in Chromium in IndexedDB staat (in een lege browser op talk.parro.com:
# indexedDB.databases() = parro_db + twee firebase-databanken; OPFS leeg; Local Storage = FlutterSecureStorage-sleutels).
# Wissen = het vernieuwtoken weg: na het verlopen van het toegangstoken (1 u) was Parro uitgelogd (27-10 18:57 → 20:25Z).
# Parro bevat geen patiëntgegevens (wel kindergegevens); de sessie blijft daarom staan: IndexedDB, File System (OPFS,
# mocht Drift daarheen wisselen) en WebStorage (QuotaManager, de boekhouding van die opslag). Caches gaan wel weg.
BEHOUD_IN_PROFIEL = {"parro": frozenset({"IndexedDB", "File System", "WebStorage"})}


def wis_lijst(dienst):
    return tuple(n for n in WIS_IN_PROFIEL if n not in BEHOUD_IN_PROFIEL.get(dienst, frozenset()))


def wis_berichtcache(dienst):
    import shutil
    prof = I.GEHEIM / dienst / "profiel"
    n = 0
    for basis in [prof] + [p for p in prof.glob("*") if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile"))]:
        for naam in wis_lijst(dienst):
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
        except BrowserStartFout as e:
            code_, uitslag = 5, {"ok": False, "code": "BROWSERSTART", "sub": e.sub, "gestart": False}
            log("inlogvenster_fout", dienst=dienst, code="BROWSERSTART", soort=e.sub)
            fase("mislukt")
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
