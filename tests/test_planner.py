"""Planner: sessie kwijt na ooit ingelogd = login-alarm; proef draait niet vanzelf; klik met stille vangst = geen VNC."""
import time

from meeleespod import planner, staat, vangst, webhook


def test_sessie_kwijt_geeft_login_alarm(monkeypatch):
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    staat.schrijf("stand", {"outlook": {"ingelogd_sinds": "2026-10-01T00:00:00+00:00", "modus": "live"}})
    alarmen = []
    monkeypatch.setattr(webhook, "alarm", lambda d, soort, tekst: alarmen.append((d, soort)) or True)
    from meeleespod import outlook
    monkeypatch.setattr(outlook, "lees_sessie", lambda: None)
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: False)
    planner.draai("outlook")
    assert ("outlook", "login") in alarmen and planner.stand()["outlook"]["login_nodig"]


def test_draai_vanger_stopt_als_venster_al_dicht(monkeypatch):
    """Race: wordt het venster net gesloten (knop/timer) in het gaatje na Popen, dan mag de zichtbare vanger niet
    verweesd doordraaien met het browserslot vast."""
    import pathlib

    class NepProc:
        pid = 999999

        def poll(self):
            return None
    gestopt = []
    monkeypatch.setattr(vangst, "zorg_voor_scherm", lambda: None)
    monkeypatch.setattr(vangst, "dienstmap", lambda d: pathlib.Path("/tmp"))
    monkeypatch.setattr(vangst, "kind_omgeving", lambda d: {})
    monkeypatch.setattr(vangst.subprocess, "Popen", lambda *a, **k: NepProc())
    monkeypatch.setattr(vangst, "_stop", lambda p: gestopt.append(p))
    with vangst._venster_slot:
        vangst._venster.clear()   # geen venster meer: net gesloten
    code, uit = vangst.draai_vanger("teams", "zichtbaar", 5)
    assert code == 5 and uit["code"] == "GESTOPT" and len(gestopt) == 1


def test_proef_draait_niet_vanzelf(monkeypatch):
    staat.schrijf("stand", {"teams": {"ingelogd_sinds": "2026-10-01T00:00:00+00:00", "modus": "proef"}})
    from meeleespod import teams
    monkeypatch.setattr(teams, "ronde", lambda modus: (_ for _ in ()).throw(AssertionError("mag niet")))
    assert planner.draai("teams") is None


def test_klik_met_geldige_sessie_opent_geen_venster(monkeypatch):
    monkeypatch.setattr(vangst, "draai_vanger", lambda d, m, t: (0, {"ok": True, "code": "OK", "geldig_tot": None}))
    monkeypatch.setattr(vangst, "wis_berichtcache", lambda d: None)
    klaar = []
    uit = vangst.start_inlogvenster("outlook", lambda d, c, u: klaar.append(c))
    for _ in range(50):
        if klaar:
            break
        time.sleep(0.05)
    assert klaar == [0]
    assert vangst.aanvraag_stand(uit["id"], uit["token"])["fase"] == "klaar"
    assert vangst.aanvraag_stand(uit["id"], "verkeerd") is None
    assert not vangst.vnc_draait() and vangst.venster_stand() is None


def test_verbindfout_geeft_soort_en_host(monkeypatch):
    """B2 (27-10): 'URLError' zei niets; de stand toont nu soort + host, zonder tekst uit de fout."""
    import socket
    import urllib.error
    from meeleespod import outlook, vangnet
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    staat.schrijf("stand", {"outlook": {"ingelogd_sinds": "2026-10-01T00:00:00+00:00", "modus": "live"}})

    def stuk(req, timeout=None):
        raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(vangnet._opener, "open", stuk)
    monkeypatch.setattr(outlook, "lees_sessie", lambda: {"token": "nep"})
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: False)
    planner.draai("outlook")
    assert planner.stand()["outlook"]["laatste_ronde"]["code"] == "VERBIND_DNS_OUTLOOK"


def test_httpfout_geeft_status_en_host(monkeypatch):
    """Een 4xx/5xx (geen 401) toont nu status + host in de stand, niet de kale 'HttpFout'."""
    import urllib.error
    from meeleespod import outlook, vangnet
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    staat.schrijf("stand", {"outlook": {"ingelogd_sinds": "2026-10-01T00:00:00+00:00", "modus": "live"}})

    def stuk(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)
    monkeypatch.setattr(vangnet._opener, "open", stuk)
    monkeypatch.setattr(outlook, "lees_sessie", lambda: {"token": "nep"})
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: False)
    planner.draai("outlook")
    assert planner.stand()["outlook"]["laatste_ronde"]["code"] == "HTTP500_OUTLOOK"


def test_soort_verbindfout():
    import ssl
    import urllib.error
    from meeleespod import vangnet
    assert vangnet.soort_verbindfout(urllib.error.URLError(ssl.SSLCertVerificationError("x"))) == "CERT"
    assert vangnet.soort_verbindfout(urllib.error.URLError(ConnectionRefusedError(111, "x"))) == "GEWEIGERD"
    assert vangnet.soort_verbindfout(TimeoutError()) == "TIMEOUT"
    assert vangnet.soort_verbindfout(urllib.error.URLError(OSError(101, "x"))) == "GEEN_ROUTE"
    assert vangnet.soort_verbindfout(urllib.error.URLError("x")) == "OVERIG"
