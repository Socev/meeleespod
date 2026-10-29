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
    items, groepen, volledig = parro.haal_alles(nep_api)
    assert volledig
    ids = {i["id"] for i in items}
    assert groepen == [{"id": "111", "naam": "Groep 5b", "type": "HOME"}]
    assert {"mededeling:501", "mededeling:502", "chat:900:9001"} <= ids
    assert sum(i.startswith("agenda:70") for i in ids) == 2
    assert "mededeling:503" not in ids


def test_zaaien_en_versturen():
    items, groepen, _ = parro.haal_alles(nep_api)
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


def test_niet_zaaien_als_de_haal_onvolledig_was():
    def kapotte_chat(pad, rng):
        if pad == "/chatroom":
            raise RuntimeError("tijdelijk")
        return nep_api(pad, rng)
    items, groepen, volledig = parro.haal_alles(kapotte_chat)
    assert not volledig
    st, verstuurd = {}, []
    uit = parro.verwerk(items, groepen, st, lambda p: verstuurd.append(p) or {"ok": True}, volledig)
    assert uit["status"] == "onvolledig_niet_gezaaid" and not verstuurd and not st.get("gezaaid")
    # volgende ronde volledig: dan pas zaaien, en oud blijft oud
    items, groepen, volledig = parro.haal_alles(nep_api)
    uit = parro.verwerk(items, groepen, st, lambda p: verstuurd.append(p) or {"ok": True}, volledig)
    assert uit["gezaaid"] == 2 and "mededeling:502" not in [i["id"] for i in verstuurd[0]["items"]]


# ---- sessiemeting (wv1911): alleen namen, tijden, statussen en korte hashes ---------------------------------------------
import base64 as _b64
import json as _json


def _jwt(claims):
    deel = _b64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + deel + ".handtekening"


def test_sessiemeting_rotatie_en_geen_tokens():
    rt0, rt1, rt2 = "verzonnen-vernieuwtoken-nul-0000", _jwt({"exp": 1793000000, "iat": 1792990000}), "verzonnen-token-twee-2222"
    at = _jwt({"exp": 1792993600, "auth_time": 1792960000, "iss": "https://inlog.voorbeeld.test/realm", "sub": "x"})
    post1 = [("https://inlog.voorbeeld.test/oauth/token/1234", 200, "grant_type=refresh_token&refresh_token=" + rt0,
              _json.dumps({"access_token": at, "refresh_token": rt1, "expires_in": 3600, "refresh_expires_in": 28800}))]
    m1 = parro.meet_vernieuwing(post1, None, "2026-10-29T06:25:00+00:00")
    assert m1["vernieuwd"] and m1["http"] == 200 and m1["pad"] == "inlog.voorbeeld.test/oauth/token/N"
    assert m1["antwoord_tijden"] == {"expires_in": 3600, "refresh_expires_in": 28800}
    assert set(m1["rt_tijden"]) == {"exp", "iat"}
    assert "rotatie_bewaard" not in m1
    tekst = _json.dumps(m1)
    for geheim in (rt0, rt1, at):
        assert geheim not in tekst
    # volgende ronde stuurt rt1 (bewaard) → ja; stuurt weer rt0 (kwijt) → nee
    goed = parro.meet_vernieuwing([("https://inlog.voorbeeld.test/oauth/token", 200, _json.dumps({"refreshToken": rt1}),
                                    _json.dumps({"refreshToken": rt2, "expiresAt": "2026-10-29T09:25:00Z"}))], m1)
    assert goed["rotatie_bewaard"] is True and goed["antwoord_tijden"] == {"expiresAt": "2026-10-29T09:25:00Z"}
    kwijt = parro.meet_vernieuwing([("https://inlog.voorbeeld.test/oauth/token", 400, "refresh_token=" + rt0,
                                     _json.dumps({"error": "invalid_grant", "error_description": "Token is not active"}))], m1)
    assert kwijt["rotatie_bewaard"] is False and kwijt["fout"] == "invalid_grant" and not kwijt["vernieuwd"]
    assert "Token is not active" not in _json.dumps(kwijt)
    # geen vernieuwing gezien: de vorige hash schuift door
    leeg = parro.meet_vernieuwing([("https://rest-v2.parro.com/rest/v2/iets/77", 201, "{}", "{}")], m1)
    assert leeg["vernieuwd"] is False and leeg["rt_vorige_in"] == m1["rt_in"] and leeg["paden"] == ["rest-v2.parro.com/rest/v2/iets/N"]


def test_toegang_claims_alleen_namen():
    at = _jwt({"exp": 1792993600, "auth_time": 1792960000, "iss": "https://inlog.voorbeeld.test/realm", "email": "a@b.c"})
    c = parro.toegang_claims({"authorization": "Bearer " + at})
    assert c["claims"] == ["auth_time", "email", "exp", "iss"] and c["iss_host"] == "inlog.voorbeeld.test"
    assert "a@b.c" not in _json.dumps(c)


def test_sessiemeting_antwoordsoort_en_lengte():
    m = parro.meet_vernieuwing([("https://inloggen.voorbeeld.test/idp/oauth2/token", 400,
                                 "grant_type=refresh_token&refresh_token=verzonnen-vernieuwtoken-0000", "", "text/html; charset=utf-8")])
    assert m["http"] == 400 and m["antwoord_soort"] == "text/html" and m["antwoord_lengte"] == 0 and not m["vernieuwd"]
    assert m["verzoek_velden"] == ["grant_type", "refresh_token"]
