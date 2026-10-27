"""Omzetten naar het pc-formaat, uitsluitingen, proef/live en het bijlagenoordeel (verzonnen data, geen netwerk)."""
import json

from meeleespod import instellingen as I
from meeleespod import outlook, staat, teams, webhook


def bericht(**extra):
    m = {"Id": "AAMk-verzonnen-1", "InternetMessageId": "<abc123@voorbeeld.nl>", "ConversationId": "conv-1",
         "ParentFolderId": "map-inbox", "Subject": "Offerte koffiemachine", "ReceivedDateTime": "2026-10-26T08:00:00Z",
         "HasAttachments": True, "IsDraft": False,
         "From": {"EmailAddress": {"Name": "Voorbeeld Koffie", "Address": "info@voorbeeld.nl"}},
         "ToRecipients": [{"EmailAddress": {"Name": "Dokter Test", "Address": "dokter@praktijk.voorbeeld"}}],
         "Body": {"ContentType": "Text", "Content": "Beste dokter, hierbij de offerte."}, "UniqueBody": None}
    m.update(extra)
    return m


def test_naar_mail_pc_formaat():
    m = outlook.naar_mail(bericht(), {"map-inbox": "Postvak IN"})
    assert m["id"] == "abc123@voorbeeld.nl"
    assert m["map"] == "Postvak IN" and m["van_email"] == "info@voorbeeld.nl" and m["heeft_bijlagen"] is True
    assert set(m) - {"_v2id"} == {"id", "thread_id", "map", "van", "van_email", "aan", "onderwerp", "tekst", "ontvangen",
                                   "bijlagen", "heeft_bijlagen"}


def test_uitsluitingen():
    assert outlook.naar_mail(bericht(IsDraft=True), {}) is None
    assert outlook.naar_mail(bericht(**{"@odata.type": "#Microsoft.OutlookServices.EventMessage"}), {}) is None
    alias = bericht(ToRecipients=[{"EmailAddress": {"Name": "", "Address": "dokter+sb@gmail.com"}}])
    assert outlook.naar_mail(alias, {}) is None


def test_ronde_proef_en_live(monkeypatch):
    (I.GEHEIM / "outlook").mkdir(parents=True, exist_ok=True)
    (I.GEHEIM / "outlook" / "lezer.json").write_text(json.dumps(
        {"token": "nep", "geldig_tot": "2099-01-01T00:00:00Z", "anchorMailbox": "PUID:x@y", "cookies": []}))
    staat.schrijf("sleutels", {"meelees": "nep-sleutel-1234567890"})
    monkeypatch.setattr(outlook, "mappen", lambda s: ({"map-inbox": "Postvak IN", "map-junk": "Junk"}, {"map-junk"}))
    monkeypatch.setattr(outlook, "berichten_sinds", lambda s, sinds: [
        bericht(), bericht(Id="AAMk-2", InternetMessageId="<junk@x>", ParentFolderId="map-junk"),
        bericht(Id="AAMk-3", InternetMessageId="<geen-oordeel@x>", HasAttachments=False)])
    monkeypatch.setattr(outlook, "bijlage_metadata", lambda s, i: [
        {"naam": "offerte.pdf", "type": "application/pdf", "bytes": 1000, "inline": False, "id": "att-1",
         "_soort": "#Microsoft.OutlookServices.FileAttachment"}])
    gestuurd = []

    def nep_post(doel, payload, timeout=0, contact=True):
        gestuurd.append((doel, payload))
        if doel == "outlook":
            return {"ok": True, "results": [{"id": "abc123@voorbeeld.nl", "klasse": "zakelijk", "reden": "verzonnen"}]}
        return {"ok": True}
    monkeypatch.setattr(webhook, "post", nep_post)
    monkeypatch.setattr(webhook, "levensteken", lambda *a, **k: False)
    uit = outlook.ronde("proef")
    assert uit["status"] == "ok" and uit["nieuw"] == 2 and uit["overig"] == 1
    assert gestuurd[0][1]["dry_run"] is True
    assert all(not k.startswith("_") for m in gestuurd[0][1]["mails"] for k in m)
    assert staat.lees("outlook").get("gezien") == {}
    assert staat.lees("oordelen")["abc123@voorbeeld.nl"]["klasse"] == "zakelijk"
    assert set(staat.lees("oordelen")["abc123@voorbeeld.nl"]) == {"klasse", "tijd"}
    gestuurd.clear()
    uit = outlook.ronde("live")
    assert gestuurd[0][1]["dry_run"] is False
    assert set(staat.lees("outlook")["gezien"]) == {"abc123@voorbeeld.nl", "geen-oordeel@x"}
    assert not any(d == "outlook-bijlage" for d, _ in gestuurd), "bijlagen staan uit tot B3"


def test_bijlagen_alleen_bij_expliciet_oordeel(monkeypatch):
    opgehaald = []
    monkeypatch.setattr(outlook, "_get", lambda s, url, tekst=False: opgehaald.append(url) or {"ContentBytes": ""})
    mail = {"id": "m1", "thread_id": "t", "_v2id": "v", "_bijlagen_meta": [
        {"naam": "a.pdf", "id": "x", "bytes": 10, "inline": False, "_soort": "#Microsoft.OutlookServices.FileAttachment"}]}
    for oordelen in ({}, {"m1": {"klasse": "patientgebonden"}}, {"m1": {"klasse": None}}, {"m1": "zakelijk"}):
        assert outlook.bijlagen_voor_mail({}, mail, oordelen) == (0, 0)
    assert not opgehaald, "zonder expliciet zakelijk/twijfel wordt er niets opgehaald"


def test_teams_bericht_en_systeemfilter():
    b = teams.naar_bericht({"id": "1700000000001", "content": "<p>Overleg verplaatst naar 14:00</p>",
                            "imdisplayname": "Collega Voorbeeld", "originalarrivaltime": "2026-10-26T08:00:00Z",
                            "messagetype": "RichText/Html"}, "19:abc@thread.v2", "Werkoverleg")
    assert b["id"] == "1700000000001@19:abc@thread.v2" and b["tekst"] == "Overleg verplaatst naar 14:00"
    assert teams.naar_bericht({"id": "2", "content": "x", "messagetype": "ThreadActivity/AddMember"}, "c", "n") is None
    assert teams.naar_bericht({"id": "3", "content": "callStarted", "messagetype": "Text"}, "c", "n") is None


def test_teams_kanalen_uit_updates():
    raw = {"teams": [{"id": "19:algemeen@thread.tacv2", "displayName": "Praktijk", "channels": [
        {"id": "19:algemeen@thread.tacv2", "displayName": "General", "isGeneral": True},
        {"id": "19:roosters@thread.tacv2", "displayName": "Roosters", "lastMessage": {"composeTime": "2026-10-26T08:00:00Z"}}]}]}
    k = list(teams.kanalen(raw))
    assert ("19:roosters@thread.tacv2", "19:algemeen@thread.tacv2", "Roosters") == k[1][:3]
