"""Parro-wachtwoord (wv1951): opslag 0600, nooit terug te lezen, geen herhaalde pogingen na een weigering, pagina- en
API-route. Verzonnen proefaccount; geen browser (die flow zit in meeleespod/inlogtoets.py, zie onderaan)."""
import hashlib
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import threading

import pytest

SLEUTEL = "nep-toets-sleutel-0123456789abcdef"
os.environ["MEELEES_SOCEV_HASHES"] = hashlib.sha256(SLEUTEL.encode()).hexdigest()

from meeleespod import api, instellingen as I, parro, parro_inlog, staat, web  # noqa: E402

GEBRUIKER = "proefaccount@voorbeeld.invalid"
WACHTWOORD = "Verzonnen-Proef-1"


@pytest.fixture(autouse=True)
def schoon():
    parro_inlog.wis()
    staat.schrijf("parro_inlog_budget", {})
    yield
    parro_inlog.wis()
    staat.schrijf("parro_inlog_budget", {})


def test_bewaren_0600_en_nooit_in_de_stand():
    versie = parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    pad = I.GEHEIM / "parro" / "inlog.json"
    assert oct(pad.stat().st_mode & 0o777) == "0o600"
    assert parro_inlog.lees() == (GEBRUIKER, WACHTWOORD, versie)
    st = json.dumps(parro_inlog.stand())
    assert GEBRUIKER not in st and WACHTWOORD not in st and "voorbeeld" not in st
    assert json.loads(st)["actief"] is True
    # ook /staat bevat alleen tijden en een versienummer
    for f in I.STAAT.glob("*.json"):
        assert WACHTWOORD not in f.read_text() and GEBRUIKER not in f.read_text()


def test_ongeldige_invoer():
    for g, w in ((None, WACHTWOORD), (GEBRUIKER, ""), ("ab", WACHTWOORD), (GEBRUIKER, "a\nb"), (GEBRUIKER, "x" * 257)):
        assert parro_inlog.controleer(g, w)
        with pytest.raises(ValueError):
            parro_inlog.bewaar(g, w)


def test_geweigerd_stopt_meteen():
    v = parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    parro_inlog.registreer("GEWEIGERD", v, "ronde")
    assert parro_inlog.lees() is None
    st = parro_inlog.stand()
    assert st["geweigerd"] and not st["actief"] and st["bewaard"]
    # opnieuw invullen zet het weer aan
    parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    assert parro_inlog.lees() is not None


def poging(v, code):
    t = parro_inlog.voor_verzenden(v)
    return parro_inlog.registreer(code, v, "ronde", t)


def test_na_drie_mislukte_pogingen_gepauzeerd_en_ok_zet_terug(monkeypatch):
    monkeypatch.setattr(parro_inlog, "BUDGET", 99)
    v = parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    poging(v, "NIET_TERUG")
    poging(v, "OK")
    assert parro_inlog.stand()["fouten_op_rij"] == 0 and parro_inlog.stand()["mislukt_24u"] == 1
    parro_inlog.registreer("ANDERE_HOST", v, "ronde")   # niets verstuurd: telt niet
    for code in ("FORMULIER", "GEEN_API", "NIET_TERUG"):   # alles wat verstuurd is en niet OK eindigde telt
        poging(v, code)
    assert parro_inlog.stand()["gepauzeerd"] and parro_inlog.lees() is None


def test_budget_over_opnieuw_invullen_heen():
    """Review MOET 1: herhaald opslaan (ook door een namaakknop) zet het 24-uursbudget niet terug."""
    for i in range(parro_inlog.BUDGET):
        v = parro_inlog.bewaar(GEBRUIKER, f"Fout-Wachtwoord-{i}")
        assert parro_inlog.lees() is not None
        poging(v, "GEWEIGERD")
    parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    st = parro_inlog.stand()
    assert parro_inlog.lees() is None and st["budget_op"] and not st["actief"] and st["mislukt_24u"] == parro_inlog.BUDGET
    # na 24 uur weer ruimte
    b = staat.lees("parro_inlog_budget")
    b["mislukt"] = ["2020-01-01T00:00:00+00:00"] * parro_inlog.BUDGET
    staat.schrijf("parro_inlog_budget", b)
    assert parro_inlog.lees() is not None


def test_crash_na_verzenden_telt_als_mislukt():
    v = parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    parro_inlog.voor_verzenden(v)   # daarna geen registreer (podstop)
    assert parro_inlog.stand()["mislukt_24u"] == 1 and parro_inlog.stand()["fouten_op_rij"] == 1


def test_gebruikersnaam_ligt_vast_tot_vrijgeven():
    parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    parro_inlog.bewaar(GEBRUIKER.upper(), "Nieuw-Wachtwoord-2")   # zelfde naam, ander wachtwoord: mag
    parro_inlog.wis()
    with pytest.raises(parro_inlog.AndereGebruiker):
        parro_inlog.bewaar("ander-account@voorbeeld.invalid", WACHTWOORD)   # wissen laat de naam vastliggen
    parro_inlog.gebruiker_vrijgeven()
    parro_inlog.bewaar("ander-account@voorbeeld.invalid", WACHTWOORD)
    assert GEBRUIKER not in json.dumps(staat.lees("parro_inlog_budget"))


def test_uitkomst_van_een_oud_wachtwoord_telt_niet():
    oud = parro_inlog.bewaar(GEBRUIKER, "Oud-Wachtwoord-1")
    parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    assert parro_inlog.registreer("GEWEIGERD", oud, "ronde") is None
    assert parro_inlog.lees() is not None


def test_herstel_zonder_wachtwoord_vult_niets_in(monkeypatch):
    """Zonder bewaard wachtwoord: geen poging (pc: geen keyring = alleen alarm)."""
    class Pagina:
        url = "https://inloggen.parnassys.net/idp/"
    monkeypatch.setattr(parro, "probeer_inloggen", lambda *a, **k: pytest.fail("niet invullen"))
    assert parro.herstel(Pagina(), "ronde") == ({}, None, "GEEN_WACHTWOORD")


def test_herstel_registreert_en_telt_vooraf(monkeypatch):
    class Pagina:
        url = "https://inloggen.parnassys.net/idp/"
    v = parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    alarmen = []
    monkeypatch.setattr(parro.webhook, "alarm", lambda *a: alarmen.append(a[1]) or True)

    def nep(page, login, voor_verzenden=None, terug_wacht_s=None):
        assert login == (GEBRUIKER, WACHTWOORD, v)
        return {}, "GEWEIGERD", voor_verzenden()
    monkeypatch.setattr(parro, "probeer_inloggen", nep)
    assert parro.herstel(Pagina(), "ronde") == ({}, None, "GEWEIGERD")
    st = parro_inlog.stand()
    assert st["geweigerd"] and st["mislukt_24u"] == 1 and alarmen == ["wachtwoord-fout"]


def test_probeer_inloggen_alleen_op_de_parnassys_host():
    class Pagina:
        def __init__(self, url):
            self.url = url

        def __getattr__(self, naam):
            pytest.fail("mag de pagina niet aanraken: " + naam)
    for url in ("https://inloggen.parnassys.net.kwaad.example/idp/", "http://inloggen.parnassys.net/idp/",
                "https://kwaad.example/inloggen.parnassys.net", "https://talk.parro.com/"):
        assert parro.probeer_inloggen(Pagina(url), (GEBRUIKER, WACHTWOORD), lambda: pytest.fail("telt")) == \
            ({}, "ANDERE_HOST", None)


# ---- pagina en API ----------------------------------------------------------------------------------------------------
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


KNOP = {"X-Meelees-Knop": "1", "Host": "meelees.voorbeeld", "Origin": "https://meelees.voorbeeld",
        "Content-Type": "application/json"}


def test_pagina_bewaart_en_toont_nooit_terug(poorten, monkeypatch):
    _, pw = poorten
    proeven = []
    monkeypatch.setattr(parro, "start_proef", lambda: proeven.append(1) or True)
    body = json.dumps({"gebruiker": GEBRUIKER, "wachtwoord": WACHTWOORD})
    assert vraag(pw, "POST", "/parro/wachtwoord", body)[0] == 403
    assert vraag(pw, "POST", "/parro/wachtwoord", body, {**KNOP, "Origin": "https://kwaad.voorbeeld"})[0] == 403
    assert vraag(pw, "POST", "/parro/wachtwoord", json.dumps({"gebruiker": GEBRUIKER}), KNOP)[0] == 400
    assert vraag(pw, "POST", "/parro/wachtwoord", json.dumps({"gebruiker": GEBRUIKER, "wachtwoord": WACHTWOORD,
                                                               "url": "https://elders"}), KNOP)[0] == 400
    assert vraag(pw, "GET", "/parro/wachtwoord")[0] == 404
    st, data = vraag(pw, "POST", "/parro/wachtwoord", body, KNOP)
    assert st == 200 and json.loads(data) == {"ok": True, "proef": True} and proeven == [1]
    assert parro_inlog.lees()[:2] == (GEBRUIKER, WACHTWOORD)
    st, data = vraag(pw, "GET", "/stand.json")
    assert st == 200 and json.loads(data)["parro_wachtwoord"]["bewaard"] is True
    assert WACHTWOORD.encode() not in data and GEBRUIKER.encode() not in data
    st, html = vraag(pw, "GET", "/")
    assert b'id="parro-ww-form"' in html and WACHTWOORD.encode() not in html
    ander = json.dumps({"gebruiker": "ander-account@voorbeeld.invalid", "wachtwoord": WACHTWOORD})
    st, data = vraag(pw, "POST", "/parro/wachtwoord", ander, KNOP)
    assert st == 409 and b"voorbeeld" not in data and parro_inlog.lees()[0] == GEBRUIKER
    assert vraag(pw, "POST", "/parro/wachtwoord/wis", b"", KNOP)[0] == 200
    assert parro_inlog.lees() is None and not (I.GEHEIM / "parro" / "inlog.json").exists()
    st, html = vraag(pw, "GET", "/")
    assert b'method="post"' in html and b'name="password"' not in html


def test_api_stand_en_proef(poorten, monkeypatch):
    pa, _ = poorten
    k = {"Authorization": "Bearer " + SLEUTEL}
    monkeypatch.setattr(parro, "start_proef", lambda: True)
    assert vraag(pa, "POST", "/api/parro-proef", b"")[0] == 401
    assert vraag(pa, "POST", "/api/parro-proef", b"", k)[0] == 409   # niets bewaard
    parro_inlog.bewaar(GEBRUIKER, WACHTWOORD)
    st, data = vraag(pa, "GET", "/api/stand", koppen=k)
    w = json.loads(data)["diensten"]["parro"]["wachtwoord"]
    assert w["bewaard"] and w["actief"] and WACHTWOORD.encode() not in data and GEBRUIKER.encode() not in data
    assert vraag(pa, "POST", "/api/parro-proef", b"", k)[0] == 202
    assert vraag(pa, "POST", "/api/parro-proef", b"", k)[0] == 429
    assert vraag(pa, "POST", "/api/parro-gebruiker-vrij", b"")[0] == 401
    assert vraag(pa, "POST", "/api/parro-gebruiker-vrij", b"", k)[0] == 200
    assert not staat.lees("parro_inlog_budget").get("naam")


@pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="geen Playwright (draait in de rooktest)")
def test_inlogtoets_met_nagemaakte_paginas():
    r = subprocess.run([sys.executable, "-m", "meeleespod.inlogtoets"], capture_output=True, text=True, timeout=600,
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert r.returncode == 0, r.stdout[-2000:]
    assert WACHTWOORD not in r.stdout and WACHTWOORD not in r.stderr
