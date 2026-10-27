"""Parro-lezer droog tegen een verzonnen API-antwoord (geen browser, geen netwerk)."""
from datetime import datetime, timedelta, timezone

from meeleespod import parro

RECENT = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

GROEPEN = {"items": [{"name": "Groep 5b", "type": "HOME", "links": [{"rel": "self", "id": 111}]}]}
MEDEDELINGEN = {"items": [
    {"title": "Schoolreis", "contents": "<p>Neem een lunchpakket mee.</p>", "createdAt": RECENT,
     "owner": {"firstname": "Juf", "surname": "Voorbeeld"}, "links": [{"rel": "self", "id": 501}],
     "attachments": [{"name": "brief.pdf"}]},
    {"title": "Oud", "contents": "al verstuurd door de pc", "createdAt": "2026-10-24T09:00:00+02:00",
     "links": [{"rel": "self", "id": 502}]},
    {"title": "Concept", "contents": "x", "draft": True, "links": [{"rel": "self", "id": 503}]},
]}
AGENDA = {"items": [
    {"calendarItem": {"title": "Ouderavond", "startDate": (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%dT19:30:00+01:00"), "endDate": "2026-11-12T21:00:00+01:00",
                      "type": "EVENT", "description": "In de aula"}, "dayNumber": 1, "links": [{"rel": "self", "id": 701}],
     "groups": [{"name": "Groep 5b"}]},
    {"calendarItem": {"title": "Studiedag", "startDate": (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%dT00:00:00+01:00"), "type": "EVENT"}, "dayNumber": 1,
     "createdAt": RECENT, "links": [{"rel": "self", "id": 702}]},
]}
ROOMS = {"items": [{"name": "Juf Voorbeeld", "type": "PERSONAL", "links": [{"rel": "self", "id": 900}]}]}
CHAT = {"items": [{"text": "Tot morgen!", "createdAt": RECENT, "identity": {"firstname": "Juf"},
                   "links": [{"rel": "self", "id": 9001}]}]}


def nep_api(pad, rng):
    if pad.startswith("/group"):
        return GROEPEN
    if "RAnnouncementEventPrimer" in pad:
        return MEDEDELINGEN
    if "RCalendarItemEventPrimer" in pad:
        return AGENDA
    if pad == "/chatroom":
        return ROOMS
    if pad.startswith("/chatroom/900/chatmessage"):
        return CHAT
    raise AssertionError(pad)


def test_ids_zoals_de_pc():
    items, groepen = parro.haal_alles(nep_api)
    ids = {i["id"] for i in items}
    assert groepen == [{"id": "111", "naam": "Groep 5b", "type": "HOME"}]
    assert {"mededeling:501", "mededeling:502", "chat:900:9001"} <= ids
    assert sum(i.startswith("agenda:70") for i in ids) == 2
    assert "mededeling:503" not in ids


def test_zaaien_en_versturen():
    items, groepen = parro.haal_alles(nep_api)
    st, verstuurd = {}, []
    uit = parro.verwerk(items, groepen, st, lambda p: verstuurd.append(p) or {"ok": True})
    ids = [i["id"] for i in verstuurd[0]["items"]]
    # na de laatste pc-ronde (25-10 15:17) aangemaakt: mededeling 501, chat, agenda 702 (createdAt); 502 en 701 gezaaid
    assert sorted(i.split(":")[0] + ":" + i.split(":")[1] for i in ids) == ["agenda:702", "chat:900", "mededeling:501"]
    assert all("aangemaakt" not in i for i in verstuurd[0]["items"]), "intern veld mag niet mee"
    assert uit["gezaaid"] == 2 and uit["verstuurd"] == 3
    # tweede ronde: niets nieuws
    verstuurd.clear()
    uit2 = parro.verwerk(items, groepen, st, lambda p: verstuurd.append(p) or {"ok": True})
    assert uit2["verstuurd"] == 0 and not verstuurd


def test_geen_groepen_geen_verzending():
    st = {"gezaaid": "x"}
    uit = parro.verwerk([], [], st, lambda p: (_ for _ in ()).throw(AssertionError("niet sturen")))
    assert uit["status"] == "geen_groepen"
