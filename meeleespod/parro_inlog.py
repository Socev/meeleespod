"""Parro-wachtwoord voor zelf opnieuw inloggen (wv1951; David 29-10-2026 Ja op #parro-wachtwoord).

Waarom: het vernieuwtoken van ParnasSys is 8 u geldig vanaf de inlog (gemeten 29-10 11:16Z: JWT exp = iat + 8 u) en
stil vernieuwen verlengt dat niet (28-10: uitval 8 u na elke inlog). De werk-pc logde dan zelf opnieuw in met
gebruikersnaam en wachtwoord uit Windows Credential Manager (keyring); dit is dezelfde route op de pod.

- Opslag alleen in /geheim/parro/inlog.json (0600, app-cache, niet in de NAS-back-up). David vult het zelf in op de
  inlogpagina (POST, nooit terug te lezen); /staat/parro_inlog.json bevat alleen tijden, codes en een versienummer.
- Nooit in stand, log, alarm of webhook: geen gebruikersnaam, geen wachtwoord, geen lengte.
- Geen herhaalde pogingen met een fout wachtwoord (anders blokkeert ParnasSys het account): bij GEWEIGERD (de
  ParnasSys-foutmelding onder het formulier) stopt het zelf inloggen meteen; na MAX_FOUTEN andere mislukte pogingen op
  rij ook. Pas een nieuw ingevuld wachtwoord zet het weer aan.
- Review wv1951 MOET 1: een ander proces op Olares kan de knop van de inlogpagina namaken (LEESMIJ, B2). Daarom staat
  daarnaast een BUDGET dat opnieuw invullen NIET terugzet: hooguit BUDGET verzendingen naar ParnasSys per 24 u die niet
  OK eindigden (geteld vóór het versturen, dus ook bij een crash), en de gebruikersnaam ligt vast na de eerste keer
  (alleen als HMAC met een eigen zout); een andere naam kan pas na vrijgeven met de Socev-sleutel (/api/parro-gebruiker-vrij).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import instellingen as I
from . import staat
from .logboek import log

# Uitkomst van één poging (witte lijst; nooit fouttekst):
#   OK          terug in de app met API-verkeer
#   GEWEIGERD   ParnasSys toonde de foutmelding onder het formulier (onjuist, geblokkeerd of inactief)
#   ANDERE_HOST de pagina stond niet op de ParnasSys-inloghost: niets ingevuld
#   GEEN_VELD   geen zichtbaar invulveld: niets ingevuld
#   FORMULIER   fout tijdens het invullen
#   NIET_TERUG  na het versturen niet binnen de tijd terug in de app (bv. een extra stap)
#   GEEN_API    terug in de app maar geen API-verkeer gezien
CODES = ("OK", "GEWEIGERD", "ANDERE_HOST", "GEEN_VELD", "FORMULIER", "NIET_TERUG", "GEEN_API")
MAX_FOUTEN = 3          # per bewaard wachtwoord: zoveel verzendingen op rij die niet OK eindigden
BUDGET = 3              # over alles heen: zoveel niet-OK-verzendingen per 24 u (opnieuw invullen zet dit niet terug)


class AndereGebruiker(ValueError):
    """Er ligt al een andere gebruikersnaam vast."""
MOMENTEN = ("ronde", "stil", "proef")
_STUURTEKEN = re.compile(r"[\x00-\x1f\x7f]")


def nu_iso():
    return datetime.now(timezone.utc).isoformat()


def _bestand():
    return I.GEHEIM / "parro" / "inlog.json"


def controleer(gebruiker, wachtwoord):
    """Geeft None als de invoer bruikbaar is, anders een korte foutzin (zonder de waarde)."""
    if not isinstance(gebruiker, str) or not isinstance(wachtwoord, str):
        return "gebruikersnaam en wachtwoord zijn verplicht"
    if not 3 <= len(gebruiker.strip()) <= 254 or _STUURTEKEN.search(gebruiker):
        return "de gebruikersnaam (je ParnasSys-e-mailadres) heeft geen geldige vorm"
    if not 1 <= len(wachtwoord) <= 256 or _STUURTEKEN.search(wachtwoord):
        return "het wachtwoord heeft geen geldige vorm"
    return None


def _budget():
    b = staat.lees("parro_inlog_budget")
    grens = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    b["mislukt"] = [t for t in (b.get("mislukt") or []) if isinstance(t, str) and t >= grens]
    return b


def _naam_hmac(b, gebruiker):
    if not b.get("zout"):
        b["zout"] = secrets.token_hex(16)
    return hmac.new(bytes.fromhex(b["zout"]), gebruiker.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


def gebruiker_vrijgeven():
    """Alleen via de API met de Socev-sleutel: een andere gebruikersnaam mag daarna één keer vastgelegd worden."""
    staat.werk_bij("parro_inlog_budget", lambda b: b.pop("naam", None))
    log("parro_gebruiker_vrij", status="ok")


def bewaar(gebruiker, wachtwoord):
    """Schrijft de inlog atomair met 0600 en zet de pogingenstand van dit wachtwoord terug (het budget niet). Geeft het
    nieuwe versienummer. AndereGebruiker als er al een andere gebruikersnaam vastligt."""
    fout = controleer(gebruiker, wachtwoord)
    if fout:
        raise ValueError(fout)

    def naam(b):
        h = _naam_hmac(b, gebruiker)
        if b.get("naam") and not hmac.compare_digest(b["naam"], h):
            return False
        b["naam"] = h
        return True
    if not staat.werk_bij("parro_inlog_budget", naam):
        log("parro_wachtwoord_geweigerd", reden="andere_gebruiker")
        raise AndereGebruiker("er ligt al een andere gebruikersnaam vast; laat die eerst vrijgeven")
    doel = _bestand()
    doel.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(doel.parent, 0o700)
    versie = secrets.token_hex(4)
    tmp = doel.with_name(f".inlog.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"gebruiker": gebruiker.strip(), "wachtwoord": wachtwoord, "versie": versie}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, doel)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    staat.schrijf("parro_inlog", {"gezet": nu_iso(), "versie": versie, "fouten_op_rij": 0, "geweigerd": None,
                                  "laatste": None, "proef": None})
    log("parro_wachtwoord_bewaard", status="ok")
    return versie


def wis():
    try:
        _bestand().unlink()
    except FileNotFoundError:
        pass
    staat.schrijf("parro_inlog", {"gewist": nu_iso()})
    log("parro_wachtwoord_gewist", status="ok")


def _lees_bestand():
    try:
        with open(_bestand(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or controleer(d.get("gebruiker"), d.get("wachtwoord")):
        return None
    return d


def lees():
    """(gebruiker, wachtwoord, versie) als zelf inloggen mag; anders None (niets bewaard, geweigerd of te vaak mislukt)."""
    d = _lees_bestand()
    if not d:
        return None
    st = staat.lees("parro_inlog")
    if st.get("versie") != d.get("versie") or st.get("geweigerd") or int(st.get("fouten_op_rij") or 0) >= MAX_FOUTEN:
        return None
    if len(_budget()["mislukt"]) >= BUDGET:
        return None
    return d["gebruiker"], d["wachtwoord"], d["versie"]


def voor_verzenden(versie):
    """Vlak vóór het versturen van het wachtwoord: telt als mislukt tot registreer(OK) het terugdraait (review ZOU 3: ook
    een crash of podherstart tussen versturen en uitslag telt dan mee). Geeft het tijdstempel voor registreer."""
    t = nu_iso()
    staat.werk_bij("parro_inlog_budget", lambda b: b.update(mislukt=_budget()["mislukt"] + [t]))

    def f(st):
        if st.get("versie") == versie:
            st["fouten_op_rij"] = int(st.get("fouten_op_rij") or 0) + 1
    staat.werk_bij("parro_inlog", f)
    return t


def registreer(code, versie, moment, verzonden=None):
    """Uitkomst van een poging vastleggen (alleen voor de versie waarmee geprobeerd is). verzonden = tijdstempel van
    voor_verzenden (None = er is niets verstuurd). Geeft de nieuwe stand, of None bij een oude versie."""
    code = code if code in CODES else "FORMULIER"
    moment = moment if moment in MOMENTEN else "ronde"
    if code == "OK" and verzonden:
        staat.werk_bij("parro_inlog_budget", lambda b: b.update(mislukt=[t for t in _budget()["mislukt"] if t != verzonden]))

    def f(st):
        if st.get("versie") != versie:
            return False   # intussen een nieuw wachtwoord ingevuld: deze uitkomst hoort bij het oude
        st["laatste"] = {"tijd": nu_iso(), "code": code, "moment": moment}
        if code == "OK":
            st["fouten_op_rij"] = 0
            st["laatst_gelukt"] = st["laatste"]["tijd"]
        elif code == "GEWEIGERD":
            st["geweigerd"] = st["laatste"]["tijd"]
        return True
    toegepast = staat.werk_bij("parro_inlog", f)
    log("parro_wachtwoord_poging", code=code, stap=moment, status="ok" if toegepast else "oud")
    return stand() if toegepast else None


def zet_proef(uitslag, versie=None):
    """Proefuitslag bij het wachtwoord waarmee geproefd is (review ZOU 6); None = proef zonder wachtwoord."""
    def f(st):
        if versie is not None and st.get("versie") != versie:
            return
        st["proef"] = {"tijd": nu_iso(), **{k: v for k, v in uitslag.items() if k in ("code", "route")}}
    staat.werk_bij("parro_inlog", f)


def stand():
    """Voor de inlogpagina en /api/stand: alleen ja/nee, tijden en codes."""
    st = staat.lees("parro_inlog")
    bewaard = _lees_bestand() is not None and st.get("versie") is not None
    uit = {"bewaard": bewaard, "gezet": st.get("gezet") if bewaard else None, "geweigerd": st.get("geweigerd"),
           "gepauzeerd": int(st.get("fouten_op_rij") or 0) >= MAX_FOUTEN, "fouten_op_rij": int(st.get("fouten_op_rij") or 0),
           "laatste": st.get("laatste"), "laatst_gelukt": st.get("laatst_gelukt"), "proef": st.get("proef"),
           "gewist": st.get("gewist"), "mislukt_24u": len(_budget()["mislukt"])}
    uit["budget_op"] = uit["mislukt_24u"] >= BUDGET
    uit["actief"] = bewaard and not uit["geweigerd"] and not uit["gepauzeerd"] and not uit["budget_op"]
    return uit
