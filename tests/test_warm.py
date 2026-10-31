"""wv2066: Outlook en Teams warm houden (stille vangst als de vorige geslaagde ≥ WARM_UUR geleden is) en de vangstlog."""
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from meeleespod import api, planner, staat, vangst, webhook
from meeleespod import instellingen as I

TS = (pathlib.Path(__file__).resolve().parents[1] / "vanger" / "src" / "vanger.ts").read_text()


def _iso(uur_geleden):
    return (datetime.now(timezone.utc) - timedelta(hours=uur_geleden)).isoformat()


@pytest.fixture
def warm(monkeypatch):
    monkeypatch.setattr(I, "WARM_UUR", 3.0)
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    staat.schrijf("stand", {d: {"ingelogd_sinds": "2026-10-31T07:00:00+00:00", "modus": "live"} for d in ("outlook", "teams")})
    staat.schrijf("vangsten", {})
    monkeypatch.setattr(vangst, "wis_berichtcache", lambda d: None)
    alarmen = []
    monkeypatch.setattr(webhook, "alarm", lambda d, soort, tekst: alarmen.append((d, soort)) or True)
    return alarmen


def _nep_vanger(monkeypatch, code, uitslag):
    """Vervangt de Node-vanger, maar laat de vangstlog (bewaar_vangst) gewoon lopen."""
    aanroepen = []

    def nep(dienst, modus, timeout_s, reden=None):
        aanroepen.append((dienst, modus, reden))
        vangst.bewaar_vangst(dienst, modus, reden, uitslag)
        return code, uitslag
    monkeypatch.setattr(vangst, "draai_vanger", nep)
    return aanroepen


def test_foutnamen_gelijk_in_vanger_ts():
    blok = TS.split("const MS_FOUTNAMEN = [", 1)[1].split("]", 1)[0]
    assert tuple(re.findall(r"'([a-z_]+)'", blok)) + ("ANDERS",) == vangst.MS_FOUTNAMEN


def test_schoon_ms_witte_lijst():
    ruw = {"authorize": 3, "token": 2, "overig": "x", "fout": 1, "url": "https://login.microsoftonline.com/",
           "fouten": {"interaction_required": 1, "eyJ-iets": 4, "login_required": 0},
           "aadsts": [70044, 70044, "50058", True, 12, 700084]}
    assert vangst.schoon_ms(ruw) == {"authorize": 3, "token": 2, "overig": 0, "fout": 1,
                                     "fouten": {"interaction_required": 1}, "aadsts": [70044, 700084]}
    assert vangst.schoon_ms("tekst") is None


def test_warm_nodig(warm, monkeypatch):
    assert planner.warm_nodig("outlook")                        # nog nooit gevangen
    assert not planner.warm_nodig("parro")                      # Parro hoort er niet bij
    staat.schrijf("vangsten", {"outlook": {"laatste_ok": _iso(1)}})
    assert not planner.warm_nodig("outlook")
    staat.schrijf("vangsten", {"outlook": {"laatste_ok": _iso(2.8)}})
    assert planner.warm_nodig("outlook")                        # 15 min speling
    # een mislukte warm-poging van een uur geleden: niet elk uur opnieuw
    staat.schrijf("vangsten", {"outlook": {"laatste_ok": _iso(5), "reeks": [{"tijd": _iso(1), "reden": "warm", "ok": False}]}})
    assert not planner.warm_nodig("outlook")
    staat.schrijf("vangsten", {"outlook": {"laatste_ok": _iso(5), "reeks": [{"tijd": _iso(1), "reden": "vernieuw", "ok": False}]}})
    assert planner.warm_nodig("outlook")
    planner.zet("outlook", login_nodig=True)
    assert not planner.warm_nodig("outlook")                    # dan probeert elke ronde het al
    planner.zet("outlook", login_nodig=False)
    monkeypatch.setattr(I, "WARM_UUR", 0.0)
    assert not planner.warm_nodig("outlook")


def test_ronde_met_warmvangst_die_mislukt_geeft_geen_alarm(warm, monkeypatch):
    from meeleespod import outlook
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: False)
    monkeypatch.setattr(outlook, "ronde", lambda modus: {"status": "ok", "verstuurd": 0})
    aanroepen = _nep_vanger(monkeypatch, 4, {"ok": False, "code": "LOGIN_NODIG", "scherm": "MS_INLOG",
                                             "ms": {"authorize": 1, "token": 0, "overig": 0, "fout": 1, "fouten": {"interaction_required": 1}, "aadsts": [70044]}})
    uit = planner.draai("outlook")
    assert aanroepen == [("outlook", "stil", "warm")]
    assert uit["status"] == "ok" and warm == []                 # token nog geldig: geen login-alarm, ronde gewoon
    s = planner.stand()["outlook"]
    assert not s.get("login_nodig")
    m = staat.lees("vangsten")["outlook"]["reeks"][-1]
    assert m["reden"] == "warm" and m["code"] == "LOGIN_NODIG" and m["scherm"] == "MS_INLOG" and m["ms"]["aadsts"] == [70044]
    assert "laatste_ok" not in staat.lees("vangsten")["outlook"]
    # direct daarna geen tweede warm-poging (wel de ronde)
    aanroepen.clear()
    assert planner.draai("outlook")["status"] == "ok" and aanroepen == []


def test_geslaagde_warmvangst_zet_geldig_tot(warm, monkeypatch):
    from meeleespod import teams
    monkeypatch.setattr(teams, "vernieuw_nodig", lambda: False)
    monkeypatch.setattr(teams, "ronde", lambda modus: {"status": "ok"})
    _nep_vanger(monkeypatch, 0, {"ok": True, "code": "OK", "geldig_tot": "2026-11-01T09:00:00.000Z"})
    planner.draai("teams")
    assert planner.stand()["teams"]["geldig_tot"] == "2026-11-01T09:00:00.000Z"
    assert staat.lees("vangsten")["teams"]["laatste_ok"]
    assert not planner.warm_nodig("teams")


def test_vernieuwen_blijft_alarmeren(warm, monkeypatch):
    from meeleespod import outlook
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: True)
    aanroepen = _nep_vanger(monkeypatch, 4, {"ok": False, "code": "LOGIN_NODIG", "scherm": "MS_INLOG"})
    assert planner.draai("outlook")["status"] == "login_nodig"
    assert aanroepen == [("outlook", "stil", "vernieuw")]       # geen warm-vangst ernaast
    assert ("outlook", "login") in warm


def test_houd_warm_nacht(warm, monkeypatch):
    from meeleespod import outlook
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: False)
    monkeypatch.setattr(outlook, "ronde", lambda modus: (_ for _ in ()).throw(AssertionError("geen ronde in de nacht")))
    aanroepen = _nep_vanger(monkeypatch, 0, {"ok": True, "code": "OK", "geldig_tot": "2026-11-01T09:00:00.000Z"})
    planner.houd_warm("outlook")
    assert aanroepen == [("outlook", "stil", "warm")]
    planner.houd_warm("outlook")                                # net gevangen: niet nog eens
    assert len(aanroepen) == 1
    # token bijna op: dat is werk voor de ronde (met alarm), niet voor het warm houden
    staat.schrijf("vangsten", {})
    monkeypatch.setattr(outlook, "vernieuw_nodig", lambda: True)
    planner.houd_warm("outlook")
    assert len(aanroepen) == 1


def test_warm_gepland_alleen_buiten_ronde_uren(warm):
    nacht = datetime(2026, 11, 1, 1, 10)
    assert planner._warm_gepland("outlook", nacht, {})
    assert not planner._warm_gepland("outlook", nacht, {"warm-outlook": "2026-11-01T01"})
    assert not planner._warm_gepland("outlook", datetime(2026, 11, 1, 1, 2), {})     # vóór de minuut van de dienst
    assert not planner._warm_gepland("outlook", datetime(2026, 11, 1, 3, 10), {})    # 03:00 is een ronde-uur
    assert not planner._warm_gepland("outlook", datetime(2026, 11, 1, 12, 10), {})
    assert not planner._warm_gepland("parro", nacht, {})


def test_vangstlog_interactief_en_uren(warm):
    vangst.bewaar_vangst("outlook", "zichtbaar", "zichtbaar", {"ok": True, "code": "OK"})
    v = staat.lees("vangsten")["outlook"]
    assert v["interactief"] and v["reeks"][-1]["uur_sinds_inlog"] == 0.0
    # een geslaagde stille vangst is geen interactieve inlog
    staat.werk_bij("vangsten", lambda d: d["outlook"].__setitem__("interactief", _iso(30)))
    vangst.bewaar_vangst("outlook", "stil", "knop", {"ok": True, "code": "OK"})
    v = staat.lees("vangsten")["outlook"]
    assert v["reeks"][-1]["reden"] == "knop" and 29.9 <= v["reeks"][-1]["uur_sinds_inlog"] <= 30.1
    assert datetime.fromisoformat(v["interactief"]) < datetime.now(timezone.utc) - timedelta(hours=29)
    for _ in range(20):
        vangst.bewaar_vangst("outlook", "stil", "warm", {"ok": False, "code": "ONVOLLEDIG"})
    assert len(staat.lees("vangsten")["outlook"]["reeks"]) == vangst.VANGST_REEKS
    st = api.stand_openbaar()["diensten"]
    assert st["outlook"]["vangsten"]["warm_uur"] == 3.0 and len(st["outlook"]["vangsten"]["reeks"]) == 6
    assert "vangsten" in st["teams"] and "vangsten" not in st["parro"]
