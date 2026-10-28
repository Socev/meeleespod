"""B3b (wv1751): terugval, door lezen, herkansing, contacttijd bij herstel, Parro-sessie blijft. Verzonnen data."""
import base64
import json

import pytest

from meeleespod import instellingen as I
from meeleespod import outlook, planner, staat, vangnet, vangst, webhook
from meeleespod.toetsdata import pdf_met_tekst


@pytest.fixture(autouse=True)
def _schoon():
    """Andere toetsbestanden gaan uit van een lege staat: na elke toets weer leeg."""
    yield
    for naam in ("outlook", "oordelen", "bijlage_wachtrij", "contact", "stand", "sleutels"):
        staat.schrijf(naam, {})


def _sessie():
    (I.GEHEIM / "outlook").mkdir(parents=True, exist_ok=True)
    (I.GEHEIM / "outlook" / "lezer.json").write_text(json.dumps(
        {"token": "nep", "geldig_tot": "2099-01-01T00:00:00Z", "anchorMailbox": "PUID:x@y", "cookies": []}))
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})


def _bericht(i, bijlage=True):
    return {"Id": f"v2-{i}", "InternetMessageId": f"<m{i}@voorbeeld.nl>", "ConversationId": "c", "ParentFolderId": "in",
            "Subject": "Verzonnen offerte", "ReceivedDateTime": "2026-10-27T08:00:00Z", "HasAttachments": bijlage,
            "IsDraft": False, "From": {"EmailAddress": {"Name": "Lev", "Address": "lev@voorbeeld.nl"}}, "ToRecipients": [],
            "Body": {"Content": "Zie bijlage."}}


def _opzet(monkeypatch, klassen, bijlage_antwoord):
    """klassen: {mail-id: resultaat-dict van n8n}; bijlage_antwoord: functie(payload) -> dict of raise."""
    _sessie()
    staat.schrijf("outlook", {"gezien": {}})
    staat.schrijf("oordelen", {})
    staat.schrijf("bijlage_wachtrij", {})
    monkeypatch.setattr(I, "BIJLAGEN_AAN", True)
    monkeypatch.setattr(outlook, "mappen", lambda s: ({"in": "Postvak IN"}, set()))
    monkeypatch.setattr(outlook, "berichten_sinds", lambda s, sinds: [_bericht(i) for i in range(len(klassen))])
    monkeypatch.setattr(outlook, "bijlage_metadata", lambda s, v2: [
        {"naam": "offerte.pdf", "type": "application/pdf", "bytes": 2000, "inline": False, "id": f"att-{v2}",
         "_soort": "#Microsoft.OutlookServices.FileAttachment"}])
    pdf = base64.b64encode(pdf_met_tekst()).decode()
    opgehaald = []

    def nep_get(sessie, url, tekst=False):
        opgehaald.append(url)
        return {"@odata.type": "#Microsoft.OutlookServices.FileAttachment", "Name": "offerte.pdf", "ContentBytes": pdf}
    monkeypatch.setattr(outlook, "_get", nep_get)
    gestuurd = []

    def nep_post(doel, payload, timeout=0, contact=True):
        gestuurd.append((doel, payload, timeout))
        if doel == "outlook":
            return {"ok": True, "results": [dict(r, id=k) for k, r in klassen.items()]}
        return bijlage_antwoord(payload)
    monkeypatch.setattr(webhook, "post", nep_post)
    monkeypatch.setattr(webhook, "levensteken", lambda *a, **k: False)
    return gestuurd, opgehaald


# ---- MOET 1 -------------------------------------------------------------------------------------------------------------
def test_terugval_oordeel_niet_bewaard_en_geen_bijlage(monkeypatch):
    gestuurd, opgehaald = _opzet(monkeypatch, {
        "m0@voorbeeld.nl": {"klasse": "twijfel", "terugval": True},
        "m1@voorbeeld.nl": {"klasse": "zakelijk"}}, lambda p: {"ok": True, "door": True, "stuk_id": "s1"})
    uit = outlook.ronde("live")
    oordelen = staat.lees("oordelen")
    assert "m0@voorbeeld.nl" not in oordelen and oordelen["m1@voorbeeld.nl"]["klasse"] == "zakelijk"
    bijl = [p for d, p, _ in gestuurd if d == "outlook-bijlage"]
    assert [p["mail_id"] for p in bijl] == ["m1@voorbeeld.nl"], "terugval opent het bijlagenpad niet"
    assert uit["terugval"] == 1 and uit["doorgelaten"] == 1 and uit["geweigerd"] == 0
    assert not any("v2-0" in u for u in opgehaald), "bij terugval wordt de bijlage niet eens opgehaald"


# ---- ZOU 2 --------------------------------------------------------------------------------------------------------------
def test_door_false_telt_als_geweigerd_en_lange_timeout(monkeypatch):
    gestuurd, _ = _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}},
                         lambda p: {"ok": True, "door": False, "reden": "patientgebonden (BSN-regel)", "stuk_id": None})
    uit = outlook.ronde("live")
    assert uit["doorgelaten"] == 0 and uit["geweigerd"] == 1 and uit["wachtrij"] == 0
    assert [t for d, _, t in gestuurd if d == "outlook-bijlage"] == [outlook.BIJLAGE_TIMEOUT]
    assert outlook.BIJLAGE_TIMEOUT >= 900


def test_ok_zonder_door_is_geen_aflevering(monkeypatch):
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, lambda p: {"ok": True, "raw": True})
    uit = outlook.ronde("live")
    assert uit["doorgelaten"] == 0 and uit["geweigerd"] == 1


# ---- ZOU 1 --------------------------------------------------------------------------------------------------------------
def test_herkansing_na_503_en_opgeven_na_drie(monkeypatch):
    stand = {"n": 0}

    def storing(p):
        stand["n"] += 1
        raise vangnet.HttpFout(503, "n8n.huisdokter.dev")
    gestuurd, _ = _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, storing)
    uit = outlook.ronde("live")
    rij = staat.lees("bijlage_wachtrij")["rij"]
    assert uit["herkansing"] == 1 and uit["wachtrij"] == 1 and uit["geweigerd"] == 0
    assert set(rij[0]) == {"mail_id", "thread_id", "v2id", "bijlage_id", "pogingen", "sinds"}, "alleen ids, geen inhoud"
    assert "offerte" not in json.dumps(rij), "geen naam in de wachtrij"
    # de mail zelf is al gezien: volgende rondes sturen alleen de herkansing (review B3b ZOU 6: precies 3)
    for ronde in range(1, 3):
        uit = outlook.ronde("live")
        assert uit["herkansing"] == 1 and uit["opgegeven"] == 0, ronde
    uit = outlook.ronde("live")
    assert uit["opgegeven"] == 1 and uit["wachtrij"] == 0
    assert stand["n"] == 4, "eerste poging + precies 3 herkansingen"
    uit = outlook.ronde("live")
    assert stand["n"] == 4, "na opgeven niet meer"


def test_herkansing_slaagt_bij_volgende_ronde(monkeypatch):
    stand = {"n": 0}

    def eerst_tijdop(p):
        stand["n"] += 1
        if stand["n"] == 1:
            raise vangnet.VerbindFout("TIMEOUT", "n8n.huisdokter.dev")
        return {"ok": True, "door": True, "stuk_id": "stuk-1"}
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "twijfel"}}, eerst_tijdop)
    assert outlook.ronde("live")["herkansing"] == 1
    uit = outlook.ronde("live")
    assert uit["doorgelaten"] == 1 and uit["wachtrij"] == 0


def test_weigering_4xx_wordt_niet_herkanst(monkeypatch):
    def fout(p):
        raise vangnet.HttpFout(400, "n8n.huisdokter.dev")
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, fout)
    uit = outlook.ronde("live")
    assert uit["geweigerd"] == 1 and uit["wachtrij"] == 0


def test_sleutelfout_pauzeert_de_ronde_zonder_poging_te_tellen(monkeypatch):
    """Review B3b ZOU 8: 403 (sleutel) raakt elke bijlage → wachtrij, rest van de ronde zonder netwerk."""
    stand = {"n": 0, "sleutel_goed": False}

    def antwoord(p):
        stand["n"] += 1
        if not stand["sleutel_goed"]:
            raise vangnet.HttpFout(403, "n8n.huisdokter.dev")
        return {"ok": True, "door": True, "stuk_id": "s"}
    gestuurd, opgehaald = _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"},
                                               "m1@voorbeeld.nl": {"klasse": "zakelijk"}}, antwoord)
    uit = outlook.ronde("live")
    assert stand["n"] == 1 and uit["herkansing"] == 2 and uit["wachtrij"] == 2, "tweede bijlage zonder netwerk naar de wachtrij"
    assert len(opgehaald) == 1
    for _ in range(4):
        outlook.ronde("live")
    assert all(x["pogingen"] == 1 for x in staat.lees("bijlage_wachtrij")["rij"]), "sleutelfout telt niet als poging"
    stand["sleutel_goed"] = True
    uit = outlook.ronde("live")
    assert uit["doorgelaten"] == 2 and uit["wachtrij"] == 0


def test_geen_sleutel_pauzeert(monkeypatch):
    def geen(p):
        raise RuntimeError("geen webhooksleutel")
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, geen)
    uit = outlook.ronde("live")
    assert uit["herkansing"] == 1 and uit["geweigerd"] == 0


def test_bijlage_post_telt_niet_als_contact(monkeypatch):
    gestuurd, _ = _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, lambda p: {"ok": True, "door": True})
    contact = []
    echte = webhook.post

    def kijk(doel, payload, timeout=0, contact=True):
        if doel == "outlook-bijlage":
            assert contact is False
            kijk.n += 1
        return echte(doel, payload, timeout=timeout, contact=contact)
    kijk.n = 0
    monkeypatch.setattr(webhook, "post", kijk)
    outlook.ronde("live")
    assert kijk.n == 1


def test_volle_wachtrij_telt_als_opgegeven(monkeypatch):
    def storing(p):
        raise vangnet.HttpFout(503, "n8n.huisdokter.dev")
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, storing)
    staat.schrijf("bijlage_wachtrij", {"rij": [{"mail_id": f"x{i}", "thread_id": "", "v2id": "v", "bijlage_id": "b",
                                                 "pogingen": 1, "sinds": "x"} for i in range(outlook.WACHTRIJ_MAX)]})
    monkeypatch.setattr(outlook, "HERKANS_PER_RONDE", 0)
    uit = outlook.ronde("live")
    assert uit["opgegeven"] == 1 and uit["herkansing"] == 0


def test_herkansing_vervalt_als_oordeel_weg_is(monkeypatch):
    def storing(p):
        raise vangnet.HttpFout(502, "n8n.huisdokter.dev")
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, storing)
    outlook.ronde("live")
    staat.schrijf("oordelen", {})
    uit = outlook.ronde("live")
    assert uit["wachtrij"] == 0 and uit["geweigerd"] == 1


def test_proef_raakt_de_wachtrij_niet(monkeypatch):
    _opzet(monkeypatch, {"m0@voorbeeld.nl": {"klasse": "zakelijk"}}, lambda p: {"ok": True, "door": True})
    staat.schrijf("oordelen", {"mx@voorbeeld.nl": {"klasse": "zakelijk", "tijd": "2026-10-28T00:00:00+00:00"}})
    staat.schrijf("bijlage_wachtrij", {"rij": [{"mail_id": "mx@voorbeeld.nl", "thread_id": "", "v2id": "v", "bijlage_id": "b",
                                                 "pogingen": 1, "sinds": "x"}]})
    outlook.ronde("proef")
    assert len(staat.lees("bijlage_wachtrij")["rij"]) == 1


# ---- ZOU 6 --------------------------------------------------------------------------------------------------------------
def test_hersteld_wist_contacttijd(monkeypatch):
    monkeypatch.setattr(webhook, "wis_alarm", lambda *a: None)
    staat.schrijf("stand", {"parro": {"login_nodig": True}})
    staat.schrijf("contact", {"parro": 1234567890.0, "outlook": 1.0})
    planner.hersteld("parro")
    c = staat.lees("contact")
    assert "parro" not in c and c["outlook"] == 1.0
    assert not planner.stand()["parro"]["login_nodig"]
    # niet uitgelogd geweest: contacttijd blijft
    staat.schrijf("contact", {"parro": 5.0})
    planner.hersteld("parro")
    assert staat.lees("contact")["parro"] == 5.0


def test_na_herstel_meteen_levensteken(monkeypatch):
    import time as _t
    staat.schrijf("sleutels", {"parro": "nep-sleutel-1234567890"})
    staat.schrijf("contact", {"parro": _t.time()})
    gestuurd = []
    monkeypatch.setattr(webhook, "post", lambda doel, p, timeout=0, contact=True: gestuurd.append(p) or {"ok": True})
    assert webhook.levensteken("parro") is False, "zonder herstel: binnen 6 u geen levensteken"
    staat.schrijf("stand", {"parro": {"login_nodig": True}})
    monkeypatch.setattr(webhook, "wis_alarm", lambda *a: None)
    planner.hersteld("parro")
    assert webhook.levensteken("parro") is True and gestuurd


# ---- Parro: de sessie (IndexedDB) blijft staan ----------------------------------------------------------------------------
def test_parro_houdt_indexeddb_outlook_niet():
    for dienst in ("parro", "outlook"):
        d = I.GEHEIM / dienst / "profiel" / "Default"
        for sub in ("IndexedDB", "Cache", "Code Cache", "Service Worker", "Local Storage", "File System", "WebStorage",
                    "Session Storage"):
            (d / sub).mkdir(parents=True, exist_ok=True)
            (d / sub / "x").write_text("verzonnen")
        vangst.wis_berichtcache(dienst)
    p = I.GEHEIM / "parro" / "profiel" / "Default"
    assert (p / "IndexedDB").exists() and (p / "File System").exists() and (p / "WebStorage").exists()
    assert (p / "Local Storage").exists()
    assert not (p / "Cache").exists() and not (p / "Code Cache").exists() and not (p / "Service Worker").exists()
    assert not (p / "Session Storage").exists()
    o = I.GEHEIM / "outlook" / "profiel" / "Default"
    assert not (o / "IndexedDB").exists() and not (o / "File System").exists() and (o / "Local Storage").exists()
