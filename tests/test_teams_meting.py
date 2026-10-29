"""wv1958: Teams-meting (alleen soorten/aantallen), Teams-profiel niet meer wissen, eenmalig vers profiel."""
import json
import os
import pathlib
import re

from meeleespod import instellingen as I
from meeleespod import planner, staat, vangst, webhook

TS = (pathlib.Path(__file__).resolve().parents[1] / "vanger" / "src" / "vanger.ts").read_text()


def test_lijsten_gelijk_in_vanger_ts():
    blok = TS.split("const HOST_KLASSEN = [", 1)[1].split("]", 1)[0]
    assert tuple(re.findall(r"'([A-Z0-9_]+)'", blok)) == vangst.HOST_KLASSEN
    blok = TS.split("const MSAL_NAMEN", 1)[1].split("];", 1)[0]
    assert tuple(re.findall(r"\['([a-z0-9]+)'", blok)) == vangst.MSAL_NAMEN
    blok = TS.split("function schermSoort", 1)[1].split("\n}\n", 1)[0]
    assert set(re.findall(r"return '([A-Z_]+)'", blok)) | {"GEEN"} == set(vangst.SCHERMEN)


def test_schoon_diag_witte_lijst():
    ruw = {"metingen": 7, "zelfde_adres": 1, "herlaad": True, "nav": {"TEAMS_MCAS": 3, "https://kwaad": 2, "MS_INLOG": "x"},
           "popup_dicht": {"ANDERS": 1}, "chats_fout": {"melding": 2, "tekst": 0, "zin": "tijdelijk"},
           "msal": {"TEAMS_MCAS": ["ic3", "chatsvcagg", "eyJ-token"], "ANDERS": "graph", "vreemd": ["graph"]},
           "rt": ["TEAMS_MCAS", "http://x"], "url": "https://teams.cloud.microsoft/v2/"}
    d = vangst.schoon_diag(ruw)
    assert d == {"metingen": 7, "zelfde_adres": 1, "herlaad": 0, "nav": {"TEAMS_MCAS": 3}, "popup_dicht": {"ANDERS": 1},
                 "chats_fout": {"melding": 2, "tekst": 0}, "msal": {"TEAMS_MCAS": ["chatsvcagg", "ic3"]},
                 "rt": ["TEAMS_MCAS"]}
    assert vangst.schoon_diag("tekst") is None


def test_teams_wist_alleen_caches():
    assert vangst.wis_lijst("teams") == ("Cache", "Code Cache", "GPUCache")
    assert vangst.wis_lijst("outlook") == vangst.WIS_IN_PROFIEL
    assert "IndexedDB" not in vangst.wis_lijst("parro") and "Cache" in vangst.wis_lijst("parro")


def test_vers_profiel_eenmalig():
    prof = I.GEHEIM / "teams" / "profiel"
    (prof / "Default" / "IndexedDB").mkdir(parents=True, exist_ok=True)
    (prof / "Default" / "Cookies").write_text("oud")
    home = I.GEHEIM / "teams" / "home" / ".teams-cli"
    home.mkdir(parents=True, exist_ok=True)
    (home / "session.json").write_text("{}")
    (I.GEHEIM / "outlook" / "profiel").mkdir(parents=True, exist_ok=True)
    (I.GEHEIM / "outlook" / "profiel" / "Cookies").write_text("blijft")
    staat.schrijf("profiel_vers", {})
    assert vangst.vers_profiel_eenmalig("teams") is True
    assert prof.is_dir() and not any(prof.iterdir()) and not (home / "session.json").exists()
    (prof / "Cookies").write_text("nieuw")
    assert vangst.vers_profiel_eenmalig("teams") is False          # één keer per merk
    assert (prof / "Cookies").read_text() == "nieuw"
    assert vangst.vers_profiel_eenmalig("outlook") is False
    assert (I.GEHEIM / "outlook" / "profiel" / "Cookies").read_text() == "blijft"


class _NepProc:
    pid = os.getpid()
    returncode = 4
    uit = b""

    def communicate(self, timeout=None):
        return (self.uit, b"")


def test_onvolledig_komt_met_meting_in_de_stand(monkeypatch):
    staat.schrijf("profiel_vers", {"teams": vangst.VERS_PROFIEL["teams"]})
    staat.schrijf("teams_diag", {})
    _NepProc.uit = (json.dumps({"ok": False, "dienst": "teams", "code": "ONVOLLEDIG", "scherm": "TEAMS_CHATS_FOUT",
                                "audiences": {"chatsvcagg": False, "ic3": False, "graph": True, "x": "y"},
                                "diag": {"metingen": 3, "nav": {"TEAMS_MCAS": 4}, "chats_fout": {"melding": 1, "tekst": 0},
                                         "msal": {"TEAMS_MCAS": ["graph"]}, "rt": ["TEAMS_MCAS"], "url": "https://x"}})
                    + "\n").encode()
    monkeypatch.setattr(vangst, "zorg_voor_scherm", lambda: None)
    monkeypatch.setattr(vangst.subprocess, "Popen", lambda *a, **k: _NepProc())
    code, uit = vangst.draai_vanger("teams", "stil", 5)
    assert code == 4 and uit["code"] == "ONVOLLEDIG" and uit["scherm"] == "TEAMS_CHATS_FOUT"
    assert uit["audiences"] == {"chatsvcagg": False, "ic3": False, "graph": True}
    assert "url" not in uit["diag"] and uit["diag"]["nav"] == {"TEAMS_MCAS": 4}
    reeks = staat.lees("teams_diag")["reeks"]
    assert reeks[-1]["modus"] == "stil" and reeks[-1]["code"] == "ONVOLLEDIG" and reeks[-1]["diag"]["chats_fout"]["melding"] == 1
    # na het zichtbare venster: laatste_fout draagt scherm, audiences en meting
    planner.na_vangst("teams", code, uit)
    f = planner.stand()["teams"]["laatste_fout"]
    assert f["code"] == "ONVOLLEDIG" and f["scherm"] == "TEAMS_CHATS_FOUT" and f["audiences"]["graph"] is True
    assert f["diag"]["msal"] == {"TEAMS_MCAS": ["graph"]}
    # stil vernieuwen in een ronde: LOGIN_NODIG met de vangercode erbij
    staat.schrijf("sleutels", {"teams": "nep-sleutel-1234567890", "meelees": "nep-sleutel-1234567890"})
    staat.werk_bij("stand", lambda d: d.setdefault("teams", {}).update(ingelogd_sinds="2026-10-27T19:30:00+00:00", modus="live"))
    from meeleespod import teams
    monkeypatch.setattr(teams, "vernieuw_nodig", lambda: True)
    monkeypatch.setattr(webhook, "alarm", lambda *a, **k: True)
    monkeypatch.setattr(webhook, "gekoppeld", lambda d: True)
    monkeypatch.setattr(vangst, "wis_berichtcache", lambda d: None)
    assert planner.draai("teams")["status"] == "login_nodig"
    f = planner.stand()["teams"]["laatste_fout"]
    assert f["code"] == "LOGIN_NODIG" and f["vanger"] == "ONVOLLEDIG" and f["scherm"] == "TEAMS_CHATS_FOUT"


def test_stand_toont_teams_meting():
    from meeleespod import api
    staat.schrijf("teams_diag", {"reeks": [{"tijd": str(i), "modus": "stil", "code": "OK"} for i in range(6)]})
    d = api.stand_openbaar()
    assert [m["tijd"] for m in d["diensten"]["teams"]["diag"]] == ["2", "3", "4", "5"]
