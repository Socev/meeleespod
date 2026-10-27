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


def test_soort_verbindfout():
    import ssl
    import urllib.error
    from meeleespod import vangnet
    assert vangnet.soort_verbindfout(urllib.error.URLError(ssl.SSLCertVerificationError("x"))) == "CERT"
    assert vangnet.soort_verbindfout(urllib.error.URLError(ConnectionRefusedError(111, "x"))) == "GEWEIGERD"
    assert vangnet.soort_verbindfout(TimeoutError()) == "TIMEOUT"
    assert vangnet.soort_verbindfout(urllib.error.URLError(OSError(101, "x"))) == "GEEN_ROUTE"
    assert vangnet.soort_verbindfout(urllib.error.URLError("x")) == "OVERIG"
