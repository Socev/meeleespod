"""API en inlogpagina: sleutel, koppel zonder adres, geen VNC zonder klik, knop alleen van deze pagina."""
import hashlib
import http.client
import json
import os
import threading

import pytest

SLEUTEL = "nep-toets-sleutel-0123456789abcdef"
os.environ["MEELEES_SOCEV_HASHES"] = hashlib.sha256(SLEUTEL.encode()).hexdigest()

from meeleespod import api, staat, vangst, web  # noqa: E402


@pytest.fixture(scope="module")
def poorten():
    a, w = api.server(0), web.server(0)
    for s in (a, w):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield a.server_address[1], w.server_address[1]
    a.shutdown()
    w.shutdown()


def vraag(poort, methode, pad, body=None, koppen=None):
    c = http.client.HTTPConnection("127.0.0.1", poort, timeout=10)
    c.request(methode, pad, body=body, headers=koppen or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_api_sleutel(poorten):
    pa, _ = poorten
    assert vraag(pa, "GET", "/gezond")[0] == 200
    assert vraag(pa, "GET", "/api/stand")[0] == 401
    assert vraag(pa, "GET", "/api/stand", koppen={"Authorization": "Bearer fout-fout-fout-fout"})[0] == 401
    st, data = vraag(pa, "GET", "/api/stand", koppen={"Authorization": "Bearer " + SLEUTEL})
    assert st == 200
    s = json.loads(data)
    assert set(s["diensten"]) == {"outlook", "teams", "parro"}
    platte = json.dumps(s)
    for verboden in ("tekst", "onderwerp", "token", "secret", "van_email"):
        assert verboden not in platte


def test_koppel_zet_geen_adres(poorten):
    pa, _ = poorten
    k = {"Authorization": "Bearer " + SLEUTEL, "Content-Type": "application/json"}
    for slecht in ({"meelees": "nep-sleutel-1234567890", "url": "https://elders.example/hook"},
                   {"webhook": "https://elders.example/hook"}, {"outlook": "https://elders.example"}, {}):
        assert vraag(pa, "POST", "/api/koppel", json.dumps(slecht), k)[0] == 400
    assert vraag(pa, "POST", "/api/koppel", json.dumps({"meelees": "kort"}), k)[0] == 400
    st, data = vraag(pa, "POST", "/api/koppel", json.dumps({"meelees": "nep-sleutel-1234567890"}), k)
    assert st == 200 and json.loads(data)["gezet"] == ["meelees"]
    assert staat.lees("sleutels") == {"meelees": "nep-sleutel-1234567890"}
    from meeleespod import instellingen as I
    assert "elders" not in json.dumps(I.WEBHOOKS)
    assert vraag(pa, "POST", "/api/koppel", json.dumps({"meelees": "x" * 20}))[0] == 401


def test_ronde_en_modus(poorten):
    pa, _ = poorten
    k = {"Authorization": "Bearer " + SLEUTEL}
    assert vraag(pa, "POST", "/api/ronde?dienst=gmail", b"", k)[0] == 400
    assert vraag(pa, "POST", "/api/ronde?dienst=parro", b"", k)[0] == 202
    assert vraag(pa, "POST", "/api/ronde?dienst=parro", b"", k)[0] == 429
    assert vraag(pa, "POST", "/api/modus?dienst=outlook&modus=verzenden", b"", k)[0] == 400
    assert vraag(pa, "POST", "/api/modus?dienst=outlook&modus=live", b"", k)[0] == 200
    assert vraag(pa, "DELETE", "/api/stand", None, k)[0] == 405


def test_api_poort_heeft_geen_inlogknop(poorten):
    pa, _ = poorten
    assert vraag(pa, "POST", "/inloggen?dienst=outlook", b"", {"Authorization": "Bearer " + SLEUTEL})[0] == 404
    assert vraag(pa, "GET", "/vnc/ws?code=x")[0] == 404


def test_pagina_poort_heeft_geen_api(poorten):
    _, pw = poorten
    assert vraag(pw, "GET", "/api/stand", koppen={"Authorization": "Bearer " + SLEUTEL})[0] == 404
    st, data = vraag(pw, "GET", "/")
    assert st == 200 and b"Meeleespod" in data


def test_geen_vnc_zonder_klik(poorten):
    _, pw = poorten
    ws = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",  # gitleaks:allow (RFC 6455-voorbeeld)
          "Sec-WebSocket-Version": "13", "Host": "meelees.voorbeeld", "Origin": "https://meelees.voorbeeld"}
    assert vraag(pw, "GET", "/vnc/ws?code=", koppen=ws)[0] == 403
    assert vraag(pw, "GET", "/vnc/ws?code=verzonnen-code", koppen=ws)[0] == 403
    assert not vangst.vnc_draait()
    assert vangst.neem_vnc_code("verzonnen-code") is None


def test_knop_alleen_van_deze_pagina(poorten):
    _, pw = poorten
    assert vraag(pw, "POST", "/inloggen?dienst=outlook", b"")[0] == 403
    assert vraag(pw, "POST", "/inloggen?dienst=outlook", b"", {"X-Meelees-Knop": "1"})[0] == 403
    assert vraag(pw, "POST", "/inloggen?dienst=outlook", b"",
                 {"X-Meelees-Knop": "1", "Host": "meelees.voorbeeld", "Origin": "https://kwaad.voorbeeld"})[0] == 403
    assert not vangst.vnc_draait()


def test_novnc_geen_pad_buiten_de_map(poorten):
    _, pw = poorten
    assert vraag(pw, "GET", "/novnc/../../etc/passwd")[0] == 404


def test_hoofdpagina_heeft_venster_sluiten(poorten):
    """Een vastgelopen inlogvenster mag de pagina niet blokkeren: hoofdpagina biedt 'Venster sluiten' en roept /inloggen/stop aan."""
    _, pw = poorten
    st, pagina = vraag(pw, "GET", "/")
    assert st == 200 and b"open-venster-sluit" in pagina and "Venster sluiten".encode() in pagina
    st, js = vraag(pw, "GET", "/pagina.js")
    assert st == 200 and b"/inloggen/stop" in js and b"toonOpenVenster" in js
