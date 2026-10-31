"""wv1950: profielslot opruimen vóór elke browserstart en BROWSERSTART meetbaar (subcode uit een witte lijst)."""
import os
import pathlib
import re
import subprocess
import sys
import time

from meeleespod import instellingen as I
from meeleespod import planner, staat, vangst, webhook

SLOT_IN_LOG = ("browserType.launchPersistentContext: Target page, context or browser has been closed\nBrowser logs:\n"
               "[pid=12][err] The profile appears to be in use by another Chromium process (31) on another computer "
               "(meeleespod-oud). Chromium has locked the profile so that it doesn't get corrupted.")


def test_subcode_witte_lijst():
    assert vangst.browser_subcode(SLOT_IN_LOG) == "SLOT_IN_GEBRUIK"   # slotmelding wint van 'closed'
    assert vangst.browser_subcode("Opening in existing browser session.") == "SLOT_IN_GEBRUIK"
    assert vangst.browser_subcode("browserType.launchPersistentContext: Executable doesn't exist at /ms") == "GEEN_BINARY"
    assert vangst.browser_subcode("[err] Missing X server or $DISPLAY") == "GEEN_SCHERM"
    assert vangst.browser_subcode("launchPersistentContext: Timeout 180000ms exceeded.") == "STARTTIJD"
    assert vangst.browser_subcode("Target page, context or browser has been closed") == "DOEL_GESLOTEN"
    assert vangst.browser_subcode("iets heel anders /geheim/outlook") == "ANDERS"
    assert vangst.BrowserStartFout("vrije tekst").sub == "ANDERS"


def test_subcodes_gelijk_in_vanger_ts():
    ts = (pathlib.Path(__file__).resolve().parents[1] / "vanger" / "src" / "vanger.ts").read_text()
    blok = ts.split("function browserSubcode", 1)[1].split("\n}\n", 1)[0]
    assert tuple(re.findall(r"return '([A-Z_]+)'", blok)) == vangst.BROWSER_SUBCODES


def _profiel(dienst):
    prof = I.GEHEIM / dienst / "profiel"
    (prof / "Default" / "IndexedDB").mkdir(parents=True, exist_ok=True)
    (prof / "Default" / "Cookies").write_text("x")
    for naam in ("SingletonSocket", "SingletonCookie"):
        p = prof / naam
        if p.is_symlink() or p.exists():
            p.unlink()
        p.symlink_to("/tmp/org.chromium.Chromium.weg/" + naam)
    lock = prof / "SingletonLock"
    if lock.is_symlink() or lock.exists():
        lock.unlink()
    lock.symlink_to("meeleespod-oudepod-31")   # wijst naar een pod die niet meer bestaat (dangling symlink)
    return prof


def test_slot_weg_profiel_blijft():
    prof = _profiel("outlook")
    assert vangst.ruim_profielslot("outlook") == (0, 3)
    for naam in vangst.SLOTBESTANDEN:
        assert not (prof / naam).is_symlink() and not (prof / naam).exists()
    assert (prof / "Default" / "Cookies").read_text() == "x" and (prof / "Default" / "IndexedDB").is_dir()
    assert vangst.ruim_profielslot("outlook") == (0, 0)   # niets te doen = niets gedaan


def test_wees_browser_op_profiel_wordt_gestopt():
    prof = _profiel("teams")
    ander = I.GEHEIM / "parro" / "profiel"
    wees = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={prof}"])
    vreemd = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={ander}"])
    try:
        time.sleep(0.3)
        wezen, n = vangst.ruim_profielslot("teams")
        assert (wezen, n) == (1, 3)
        assert wees.wait(5) is not None
        assert vreemd.poll() is None   # ander profiel (andere dienst) blijft ongemoeid
    finally:
        for p in (wees, vreemd):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_laatste_fout_draagt_subcode(monkeypatch):
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    staat.schrijf("stand", {"outlook": {"ingelogd_sinds": "2026-10-01T00:00:00+00:00", "modus": "live"}})
    from meeleespod import outlook
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: True)
    monkeypatch.setattr(webhook, "alarm", lambda *a, **k: True)
    monkeypatch.setattr(vangst, "wis_berichtcache", lambda d: None)
    monkeypatch.setattr(vangst, "draai_vanger", lambda d, m, t, **k: (5, {"ok": False, "code": "BROWSERSTART",
                                                                      "sub": "SLOT_IN_GEBRUIK", "gestart": False}))
    assert planner.draai("outlook")["status"] == "vernieuw_fout"
    f = planner.stand()["outlook"]["laatste_fout"]
    assert f["code"] == "BROWSERSTART" and f["sub"] == "SLOT_IN_GEBRUIK" and f["gestart"] is False
    # een onbekende subcode komt nooit in de stand
    monkeypatch.setattr(vangst, "draai_vanger", lambda d, m, t, **k: (5, {"ok": False, "code": "BROWSERSTART",
                                                                      "sub": "/geheim/vrije tekst"}))
    planner.draai("outlook")
    assert "sub" not in planner.stand()["outlook"]["laatste_fout"]


def test_parro_startfout_wordt_subcode(monkeypatch):
    from meeleespod import parro

    class NepChromium:
        def launch_persistent_context(self, *a, **k):
            raise RuntimeError(SLOT_IN_LOG)

    class NepPw:
        chromium = NepChromium()
    _profiel("parro")
    try:
        parro._open(NepPw(), headless=True)
    except vangst.BrowserStartFout as e:
        assert e.sub == "SLOT_IN_GEBRUIK"
    else:
        raise AssertionError("geen BrowserStartFout")
    assert not (I.GEHEIM / "parro" / "profiel" / "SingletonLock").is_symlink()


def test_vanger_uitslag_alleen_witte_lijst(monkeypatch):
    class NepProc:
        pid = os.getpid()
        returncode = 5

        def communicate(self, timeout=None):
            return (b'{"ok": false, "code": "BROWSERSTART", "sub": "iets/anders", "gestart": 1, "fout": "tekst"}\n', b"")
    monkeypatch.setattr(vangst, "zorg_voor_scherm", lambda: None)
    monkeypatch.setattr(vangst.subprocess, "Popen", lambda *a, **k: NepProc())
    code, uit = vangst.draai_vanger("outlook", "stil", 5)
    assert code == 5 and uit == {"ok": False, "code": "BROWSERSTART", "sub": "ANDERS", "gestart": True}


def test_vanger_zonder_json_geeft_exitcode(monkeypatch):
    class NepProc:
        pid = os.getpid()
        returncode = -9

        def communicate(self, timeout=None):
            return (b"", b"node:internal/process Error: browserType.launchPersistentContext: Target page, context or "
                         b"browser has been closed /geheim/outlook/profiel\n")
    monkeypatch.setattr(vangst, "zorg_voor_scherm", lambda: None)
    monkeypatch.setattr(vangst.subprocess, "Popen", lambda *a, **k: NepProc())
    code, uit = vangst.draai_vanger("outlook", "stil", 5)
    assert uit == {"code": "SIGNAAL_9", "sub": "DOEL_GESLOTEN"}
    NepProc.returncode = 1
    NepProc.communicate = lambda self, timeout=None: (b"", b"")
    assert vangst.draai_vanger("outlook", "stil", 5)[1] == {"code": "EXIT_1", "sub": "ANDERS"}


def test_scherm_en_mcas_witte_lijst(monkeypatch):
    class NepProc:
        pid = os.getpid()
        returncode = 4

        def communicate(self, timeout=None):
            return (b'{"ok": false, "code": "LOGIN_NODIG", "scherm": "https://login.microsoftonline.com/x", "mcas": "2"}\n', b"")
    monkeypatch.setattr(vangst, "zorg_voor_scherm", lambda: None)
    monkeypatch.setattr(vangst.subprocess, "Popen", lambda *a, **k: NepProc())
    assert vangst.draai_vanger("outlook", "stil", 5)[1] == {"ok": False, "code": "LOGIN_NODIG", "scherm": "ANDERS", "mcas": 0}


def test_login_nodig_bewaart_scherm(monkeypatch):
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    staat.schrijf("stand", {"outlook": {"ingelogd_sinds": "2026-10-01T00:00:00+00:00", "modus": "live"}})
    from meeleespod import outlook
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: True)
    monkeypatch.setattr(webhook, "alarm", lambda *a, **k: True)
    monkeypatch.setattr(vangst, "wis_berichtcache", lambda d: None)
    monkeypatch.setattr(vangst, "draai_vanger", lambda d, m, t, **k: (4, {"ok": False, "code": "LOGIN_NODIG", "scherm": "MS_INLOG"}))
    assert planner.draai("outlook")["status"] == "login_nodig"
    f = planner.stand()["outlook"]["laatste_fout"]
    assert f["code"] == "LOGIN_NODIG" and f["scherm"] == "MS_INLOG" and planner.stand()["outlook"]["login_nodig"]
