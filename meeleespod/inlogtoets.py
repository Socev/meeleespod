"""python -m meeleespod.inlogtoets — de Parro-herstelvolgorde (wv1951) tegen nagebootste pagina's, zonder netwerk.

Alle verzoeken worden in de browser onderschept (context.route): talk.parro.com, inloggen.parnassys.net en
rest-v2.parro.com zijn kleine nagemaakte pagina's met een VERZONNEN proefaccount; al het andere wordt afgebroken. Er gaat
dus nooit een echte inlog de deur uit (ook niet in CI). Eigen tijdelijke /staat en /geheim. Draait in de rooktest (het
image heeft Playwright + Chromium) en lokaal via tests/test_parro_inlogtoets.py als Playwright er is.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_T = tempfile.mkdtemp(prefix="meelees-inlogtoets-")
os.environ["MEELEES_STAAT"] = os.path.join(_T, "staat")
os.environ["MEELEES_GEHEIM"] = os.path.join(_T, "geheim")

from . import instellingen as I  # noqa: E402
from . import parro, parro_inlog, staat, webhook  # noqa: E402

PROEF_GEBRUIKER = "proefaccount@voorbeeld.invalid"
PROEF_WACHTWOORD = "Verzonnen-Proef-1"

APP = """<!doctype html><html><body style="margin:0">
<div id="welkom" style="position:fixed;inset:0;background:#fc0">welkom (nagemaakt)</div>
<script>
if (location.hash === '#ingelogd') {
  fetch('https://rest-v2.parro.com/rest/v2/group', {headers: {authorization: 'Bearer nep.eyJleHAiOjF9.nep'}});
} else {
  // doel als nummer: een url in de query zou zelf al op 'inloggen' lijken (waarom_geen_headers kijkt naar de url)
  const doel = %(doelen)s[new URLSearchParams(location.search).get('d') || 0];
  document.getElementById('welkom').onclick = () => { if (doel) location.href = doel; };
}
</script></body></html>"""
DOELEN = ["https://inloggen.parnassys.net/idp/", "https://inloggen.parnassys.net/idp/tweestaps",
          "https://inloggen.parnassys.net/idp/verlopen", "https://inloggen.parnassys.net/idp/extra",
          "https://inloggen.parnassys.net.kwaad.example/idp/", "https://talk.parro.com/?sso=1#ingelogd",
          "https://inloggen.parnassys.net/idp/oude-fout"]
APP = APP.replace("%(doelen)s", json.dumps(DOELEN))

INLOG = """<!doctype html><html><body>
<form id="f">
  <input id="signin-email" name="emailadres" type="text" autocomplete="username" style="display:%(email)s">
  <input id="signin-password" name="wachtwoord" type="password" style="display:%(ww)s">
  <button>Inloggen</button>
</form>
<div class="feedbackPanelERROR" style="display:%(fout)s">Je e-mailadres en/of wachtwoord is onjuist (nagemaakt).</div>
<script>
const e = document.getElementById('signin-email'), w = document.getElementById('signin-password');
document.getElementById('f').onsubmit = (ev) => {
  ev.preventDefault();
  if (getComputedStyle(w).display === 'none') { w.style.display = 'inline'; return; }   // tweestaps
  const goedeNaam = %(naam_nodig)s ? e.value === %(gebruiker)s : e.value === '';
  if (goedeNaam && w.value === %(wachtwoord)s) location.href = %(na)s;
  // zoals Wicket-Ajax: eerst een POST naar de inloghost, dan pas het (bestaande) foutpaneel tonen
  else fetch('/idp/controle', {method: 'POST', body: 'x'}).then(() => {
    document.querySelector('.feedbackPanelERROR').style.display = 'block'; });
};
</script></body></html>"""


def inlogpagina(email="inline", ww="inline", naam_nodig=True, na="https://talk.parro.com/#ingelogd", fout="none"):
    return INLOG % {"email": email, "ww": ww, "naam_nodig": "true" if naam_nodig else "false", "fout": fout,
                    "gebruiker": json.dumps(PROEF_GEBRUIKER), "wachtwoord": json.dumps(PROEF_WACHTWOORD),
                    "na": json.dumps(na)}


PAGINAS = {
    "inloggen.parnassys.net/idp/": inlogpagina(),
    "inloggen.parnassys.net/idp/tweestaps": inlogpagina(ww="none"),
    "inloggen.parnassys.net/idp/verlopen": inlogpagina(email="none", naam_nodig=False),
    "inloggen.parnassys.net/idp/oude-fout": inlogpagina(email="none", naam_nodig=False, fout="block"),
    "inloggen.parnassys.net/idp/extra": inlogpagina(na="https://inloggen.parnassys.net/idp/tweede-stap"),
    "inloggen.parnassys.net/idp/tweede-stap": "<!doctype html><p>nog een stap (nagemaakt)</p>",
    "inloggen.parnassys.net.kwaad.example/idp/": inlogpagina(),
}


def _route(geladen):
    def handler(route):
        from urllib.parse import urlsplit
        u = urlsplit(route.request.url)
        sleutel = (u.hostname or "") + u.path
        geladen.append(("http" if u.scheme == "http" else "") + sleutel)
        if u.hostname == "talk.parro.com":
            return route.fulfill(status=200, content_type="text/html", body=APP)
        if u.hostname == "rest-v2.parro.com":
            return route.fulfill(status=200, content_type="application/json", body="{}",
                                 headers={"access-control-allow-origin": "*", "access-control-allow-headers": "*"})
        if sleutel == "inloggen.parnassys.net/idp/controle":
            return route.fulfill(status=200, content_type="text/plain", body="nee")
        if sleutel in PAGINAS:
            return route.fulfill(status=200, content_type="text/html", body=PAGINAS[sleutel])
        return route.abort()
    return handler


def scenario(browser, doel, wachtwoord=PROEF_WACHTWOORD):
    """Eén herstel vanaf het welkomstscherm; geeft (headers, route, code, geladen, ingevuld wachtwoordveld)."""
    ctx = browser.new_context(viewport={"width": 1100, "height": 900}, locale="nl-NL")
    geladen = []
    ctx.route("**/*", _route(geladen))
    try:
        page = ctx.new_page()
        page.goto("https://talk.parro.com/?d=%d" % DOELEN.index(doel))
        page.wait_for_timeout(300)
        headers, route, code = parro.herstel(page, "proef")
        try:
            ww = page.evaluate("() => (document.getElementById('signin-password') || {}).value || ''")
        except Exception:
            ww = ""
        return headers, route, code, geladen, ww
    finally:
        ctx.close()


def opnieuw(wachtwoord=PROEF_WACHTWOORD):
    """Elk scenario met een schone telling (het budget over 24 u wordt apart in de pytest-toetsen getoetst)."""
    staat.schrijf("parro_inlog_budget", {})
    return parro_inlog.bewaar(PROEF_GEBRUIKER, wachtwoord)


def draai(executable_path=None):
    from playwright.sync_api import sync_playwright
    # korte wachttijden: de nagemaakte pagina's reageren meteen
    parro.KLIK_HEADERS_S, parro.HEADERS_WACHT_SEC, parro.TERUG_WACHT_S = 2, 5, 6
    alarmen = []
    webhook.alarm = lambda dienst, soort, tekst: alarmen.append((dienst, soort, tekst)) or True
    uitkomst = []

    def eis(naam, waar):
        uitkomst.append((naam, bool(waar)))
        print(("  OK    " if waar else "  FOUT  ") + "inlogtoets: " + naam)

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, executable_path=executable_path)
        try:
            # A. eenstaps, juist wachtwoord
            v = opnieuw()
            h, r, c, g, _ = scenario(b, "https://inloggen.parnassys.net/idp/")
            eis("eenstaps: zelf ingelogd, API-verkeer gevangen", c == "OK" and r == "wachtwoord"
                and h.get("authorization", "").startswith("Bearer "))
            st = parro_inlog.stand()
            eis("eenstaps: stand OK zonder gebruikersnaam of wachtwoord", st["laatste"]["code"] == "OK" and st["actief"]
                and PROEF_GEBRUIKER not in json.dumps(st) and PROEF_WACHTWOORD not in json.dumps(st))

            # J. niets van de inlog blijft in een (blijvend) browserprofiel achter (review ZOU 7)
            opnieuw()
            pd = tempfile.mkdtemp(prefix="meelees-inlogtoets-profiel-")
            ctx = pw.chromium.launch_persistent_context(pd, headless=True, executable_path=executable_path,
                                                        viewport={"width": 1100, "height": 900}, locale="nl-NL")
            ctx.route("**/*", _route([]))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://talk.parro.com/?d=0")
            page.wait_for_timeout(300)
            h, r, c = parro.herstel(page, "proef")
            page.wait_for_timeout(2000)
            ctx.close()
            zoek = [x.encode(enc) for x in (PROEF_GEBRUIKER, PROEF_WACHTWOORD) for enc in ("utf-8", "utf-16-le")]
            gevonden = [f.name for f in Path(pd).rglob("*") if f.is_file() and any(z in f.read_bytes() for z in zoek)]
            eis("blijvend profiel: ingelogd, geen gebruikersnaam of wachtwoord in het profiel" + (
                f" (gevonden in {sorted(set(gevonden))[:5]})" if gevonden else ""), c == "OK" and not gevonden)
            shutil.rmtree(pd, ignore_errors=True)

            # D2. foutmelding stond er al vóór het versturen (review ZOU 2): geen onterechte weigering
            opnieuw()
            h, r, c, g, _ = scenario(b, "https://inloggen.parnassys.net/idp/oude-fout")
            eis("oude foutmelding: toch zelf ingelogd", c == "OK" and r == "wachtwoord")

            # B. fout wachtwoord → GEWEIGERD, daarna niet meer proberen
            opnieuw("Ander-Wachtwoord-2")
            h, r, c, g, _ = scenario(b, "https://inloggen.parnassys.net/idp/")
            eis("fout wachtwoord: GEWEIGERD, geen headers", c == "GEWEIGERD" and not h and r is None)
            eis("fout wachtwoord: één alarm [parro/wachtwoord-fout]", [a[1] for a in alarmen] == ["wachtwoord-fout"])
            h, r, c, g, ww = scenario(b, "https://inloggen.parnassys.net/idp/")
            eis("na weigering: niet opnieuw ingevuld (geen accountblokkade)", c == "GEEN_WACHTWOORD" and ww == "")

            # C. tweestaps
            opnieuw()
            h, r, c, g, _ = scenario(b, "https://inloggen.parnassys.net/idp/tweestaps")
            eis("tweestaps: zelf ingelogd", c == "OK" and r == "wachtwoord")

            opnieuw()
            # D. "Sessie verlopen": alleen een wachtwoordveld zichtbaar (verborgen gebruikersveld blijft leeg)
            h, r, c, g, _ = scenario(b, "https://inloggen.parnassys.net/idp/verlopen")
            eis("sessie verlopen (alleen wachtwoordveld): zelf ingelogd", c == "OK" and r == "wachtwoord")

            opnieuw()
            # E. inlogformulier op een andere host: nooit invullen
            h, r, c, g, ww = scenario(b, "https://inloggen.parnassys.net.kwaad.example/idp/")
            eis("andere host: ANDERE_HOST, wachtwoordveld leeg", c == "ANDERE_HOST" and ww == "" and not h)

            opnieuw()
            # F. SSO leeft nog: 'Inloggen' brengt de app meteen terug, geen formulier
            h, r, c, g, _ = scenario(b, "https://talk.parro.com/?sso=1#ingelogd")
            eis("SSO: terug zonder wachtwoord", c == "OK" and r == "sso"
                and not any(x.startswith("inloggen.parnassys.net") for x in g))

            # G. extra stap na het formulier → NIET_TERUG, telt als fout; na MAX_FOUTEN stopt het
            opnieuw()
            staat.schrijf("parro_inlog_budget", {"mislukt": []})
            parro_inlog.BUDGET = 99   # hier alleen de teller per wachtwoord; het budget toetst pytest
            for _ in range(parro_inlog.MAX_FOUTEN):
                h, r, c, g, _ = scenario(b, "https://inloggen.parnassys.net/idp/extra")
            st = parro_inlog.stand()
            eis("extra stap: NIET_TERUG, na 3 keer gepauzeerd", c == "NIET_TERUG" and st["gepauzeerd"] and not st["actief"]
                and parro_inlog.lees() is None)
            parro_inlog.BUDGET = 3

            # H. bestand 0600 en wissen
            pad = I.GEHEIM / "parro" / "inlog.json"
            opnieuw()
            eis("inlog.json 0600", oct(pad.stat().st_mode & 0o777) == "0o600")
            parro_inlog.wis()
            eis("wissen: bestand weg, niet meer actief", not pad.exists() and not parro_inlog.stand()["bewaard"])
            del v
        finally:
            b.close()
    # I. proef (eigen sync_playwright: niet genest): profielkopie zonder sessie (pc: --test-login); het echte profiel blijft, de kopie gaat weg
    opnieuw()
    prof = I.GEHEIM / "parro" / "profiel" / "Default"
    for naam in ("IndexedDB/parro_db", "Cookies", "Local Storage/leveldb", "Preferences"):
        (prof / naam).parent.mkdir(parents=True, exist_ok=True)
        (prof / naam).write_text("nep")
    gezien_in_kopie = {}
    echt_open = parro._open

    def open_met_routes(pw_, headless, profiel=None):
        d = profiel / "Default"
        gezien_in_kopie.update(kopie=profiel, indexeddb=(d / "IndexedDB").exists(), cookies=(d / "Cookies").exists(),
                               opslag=(d / "Local Storage").exists(), voorkeuren=(d / "Preferences").exists())
        if executable_path:
            ctx = pw_.chromium.launch_persistent_context(str(profiel), headless=True, executable_path=executable_path,
                                                         viewport={"width": 1100, "height": 900}, locale="nl-NL")
        else:
            ctx = echt_open(pw_, headless, profiel)
        ctx.route("**/*", _route([]))
        return ctx
    parro._open = open_met_routes
    uit = parro.proef_inlog()
    parro._open = echt_open
    print("  proef-uitslag:", uit)
    eis("proef: zelf ingelogd in de kopie", uit == {"code": "OK", "route": "wachtwoord"}
        and parro_inlog.stand()["proef"]["code"] == "OK")
    eis("proef: kopie zonder parro_db, cookies en webopslag", gezien_in_kopie.get("voorkeuren") and not any(
        gezien_in_kopie.get(k) for k in ("indexeddb", "cookies", "opslag")))
    eis("proef: kopie weg, echt profiel ongemoeid", gezien_in_kopie.get("kopie") is not None and not gezien_in_kopie["kopie"].exists()
        and (prof / "IndexedDB" / "parro_db").read_text() == "nep" and (prof / "Cookies").exists())

    parro_inlog.wis()
    shutil.rmtree(_T, ignore_errors=True)
    fout = [n for n, ok in uitkomst if not ok]
    print("inlogtoets:", "GESLAAGD" if not fout else f"{len(fout)} FOUT")
    return 0 if not fout else 1


if __name__ == "__main__":
    sys.exit(draai(os.environ.get("MEELEES_TOETS_CHROMIUM") or None))
