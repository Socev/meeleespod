"""Parro-lezer: parro_meelezen.py uit Socev/socev-pc (commit 30524ca), met alleen de pc-plekken anders.

Wat anders is dan op de pc (ontwerp § 4, parro-lezer):
- socev_pc.py → meeleespod: datamap = /staat (gezien-lijst) en /geheim/parro (browserprofiel), sleutel via /api/koppel,
  versie = image-commit.
- Wachtwoord (wv1951, David 29-10 Ja): niet uit Windows Credential Manager maar uit /geheim/parro/inlog.json, door David
  zelf ingevuld op de inlogpagina (parro_inlog.py). Herstelvolgorde zoals de pc: geen API-verkeer → klik 'Inloggen'
  (ParnasSys-SSO) → op de ParnasSys-inlogpagina zelf inloggen. Zonder bewaard wachtwoord: één alarm + inlogpagina.
- Geen HTML-overzicht, geen schermafdruk, geen ontdek-map: er komt geen Parro-tekst op schijf.
- Alle API-aanroepen gaan eerst langs het vangnet (alleen GET naar rest-v2.parro.com, geen redirects).
- Zaaien (ontwerp § 8.2): de eerste ronde zet alles van vóór de laatste pc-ronde op gezien zonder te versturen.
De leeslogica (groepen, mededelingen, agenda, gesprekken, ids) is ongewijzigd, zodat de ids gelijk zijn aan de pc.
"""
import base64
import hashlib
import html
import json
import os
import re
import secrets
import signal
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from . import instellingen as I
from . import parro_inlog, staat, vangnet, vangst, webhook
from .logboek import log

APP_URL = "https://talk.parro.com/"
API = "https://rest-v2.parro.com/rest/v2"
VENSTER_DAGEN = 21
WEBHOOK_TIMEOUT = 300
STATE_BEWAAR_DAGEN = 120
AGENDA_DAGEN_VOORUIT = 120
CHAT_BERICHTEN = 30
HEADERS_WACHT_SEC = 90
KLIK_HEADERS_S = 45          # na de klik op 'Inloggen': zoveel seconden op API-verkeer wachten (SSO)
POPUP_HOSTS = (re.compile(r"(^|\.)parro\.com$", re.I), re.compile(r"(^|\.)parnassys\.net$", re.I))
INTERN = ("aangemaakt",)   # velden die de pod zelf gebruikt en niet meestuurt (payload blijft gelijk aan de pc)


# ===== ongewijzigd uit parro_meelezen.py (op logging na) ================================================================
def strip_html(t):
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p\s*>", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def dedupe(items):
    gezien, uit = set(), []
    for it in items:
        sleutel = (it["id"], it["tekst"][:80])
        if sleutel in gezien:
            continue
        gezien.add(sleutel)
        uit.append(it)
    return uit


def luister_headers(page):
    """Begint te luisteren; het dict vult zich bij de eerste API-aanroep van de app zelf."""
    gevangen = {}

    def on_request(req):
        if "rest-v2.parro.com" in req.url and req.headers.get("authorization") and not gevangen:
            for k in ("authorization", "accept", "parro-authorization-role", "parro-app-version", "content-type"):
                if req.headers.get(k):
                    gevangen[k] = req.headers[k]
    page.on("request", on_request)
    return gevangen


def vang_headers(page, seconden=45, gevangen=None):
    if gevangen is None:
        gevangen = luister_headers(page)
    t0 = time.time()
    while time.time() - t0 < seconden and not gevangen:
        page.wait_for_timeout(500)
    return gevangen


def token_info(headers):
    try:
        tok = headers.get("authorization", "").split(" ", 1)[1]
        deel = tok.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(deel + "=" * (-len(deel) % 4)))
        uit = {}
        for k in ("iat", "exp"):
            if isinstance(payload.get(k), (int, float)):
                uit[k] = datetime.fromtimestamp(payload[k], timezone.utc).isoformat()
        return uit
    except Exception:
        return {}


def klik_inloggen(page):
    """Welkomstscherm van de app: 'Inloggen' (Flutter-canvas, vaste positie bij viewport 1100x900). Leeft de SSO-sessie
    bij ParnasSys nog, dan is de app zonder wachtwoord weer binnen; anders wachten tot de inlogpagina er staat (pc)."""
    gevangen = luister_headers(page)   # vóór de klik: een snelle SSO-terugkeer doet zijn eerste API-aanroep binnen 4 s
    try:
        page.mouse.click(550, 570)
        page.wait_for_timeout(4000)
    except Exception:
        return {}
    headers = vang_headers(page, KLIK_HEADERS_S, gevangen)
    if headers:
        return headers
    try:
        if _host(page) == APP_HOST:
            page.wait_for_url(re.compile(r"parnassys|login|oauth"), timeout=15000)
    except Exception:
        pass
    return {}


# ---- zelf inloggen met het bewaarde wachtwoord (wv1951; pc: probeer_inloggen + keyring_login) ------------------------
APP_HOST = "talk.parro.com"
INLOG_HOST = "inloggen.parnassys.net"   # gemeten 29-10: formulier #signin-email + #signin-password op /idp/
GEBRUIKER_VELD = ("#signin-email:visible, input[name='emailadres']:visible, input[type='email']:visible, "
                  "input[autocomplete='username']:visible, input[name*='user' i]:visible, input[name*='login' i]:visible, "
                  "input[type='text']:visible")
WACHTWOORD_VELD = "input[type='password']:visible"
FOUTMELDING = ".feedbackPanelERROR:visible"   # gemeten 29-10 met een verzonnen account: "…onjuist of je account is inactief"
TERUG_WACHT_S = 60


def _host(page):
    """Host van de pagina, alleen bij https (anders leeg)."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(page.url)
        return (u.hostname or "").lower() if u.scheme == "https" else ""
    except Exception:
        return ""


def _veld_hier(h, page):
    """Het veld hoort bij het hoofdframe van een document op https://inloggen.parnassys.net (review ZOU 4: een handle
    hoort bij precies dat document; na een doorverwijzing faalt fill op de handle in plaats van elders in te vullen)."""
    try:
        return (h is not None and h.owner_frame() == page.main_frame
                and h.evaluate("e => e.ownerDocument.location.origin") == "https://" + INLOG_HOST)
    except Exception:
        return False


def probeer_inloggen(page, login, voor_verzenden=None, terug_wacht_s=None):
    """Vult het ParnasSys-formulier in, ALLEEN op https://inloggen.parnassys.net (host vooraf, daarna per veld het
    document van de handle): één- of tweestaps, en bij "Sessie verlopen" alleen het wachtwoordveld (alleen zichtbare
    velden, zoals de pc). voor_verzenden() wordt vlak vóór het versturen aangeroepen (telling, zie parro_inlog).
    Geeft (headers, code, verzonden) met code uit parro_inlog.CODES; nooit fouttekst."""
    gebruiker, wachtwoord = login[0], login[1]
    verzonden = None
    if _host(page) != INLOG_HOST:
        return {}, "ANDERE_HOST", None
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        if _host(page) != INLOG_HOST:
            return {}, "ANDERE_HOST", None
        gv = page.locator(GEBRUIKER_VELD).first
        ww = page.locator(WACHTWOORD_VELD).first
        if gv.count():
            hg = gv.element_handle(timeout=10000)
            if not _veld_hier(hg, page):
                return {}, "ANDERE_HOST", None
            hg.fill(gebruiker, timeout=10000)
            if not ww.count():
                hg.press("Enter")   # tweestaps: eerst de gebruikersnaam
                page.wait_for_selector(WACHTWOORD_VELD, timeout=15000)
                ww = page.locator(WACHTWOORD_VELD).first
        elif not ww.count():
            return {}, "GEEN_VELD", None
        hw = ww.element_handle(timeout=10000)
        if not _veld_hier(hw, page):
            return {}, "ANDERE_HOST", None
        gevangen = luister_headers(page)
        # review ZOU 2: een foutmelding die er al stond (bv. bij "Sessie verlopen") mag niet als weigering tellen. Een
        # melding telt pas als ParnasSys op het verstuurde formulier geantwoord heeft (POST naar de inloghost, als
        # paginaverzoek of via Wicket-Ajax); het paneel zelf laten we staan (Wicket kan precies dat element vervangen).
        geantwoord = []

        def op_antwoord(r):
            try:
                from urllib.parse import urlsplit
                if r.request.method not in LEZENDE_METHODEN and (urlsplit(r.url).hostname or "").lower() == INLOG_HOST:
                    geantwoord.append(time.time())
            except Exception:
                pass
        page.on("response", op_antwoord)
        hw.fill(wachtwoord, timeout=10000)
        if voor_verzenden:
            verzonden = voor_verzenden()
        hw.press("Enter")
    except Exception:
        return {}, "FORMULIER", verzonden
    t0, terug_wacht_s = time.time(), terug_wacht_s or TERUG_WACHT_S
    while time.time() - t0 < terug_wacht_s and not gevangen:
        if _host(page) == INLOG_HOST and geantwoord and time.time() - geantwoord[0] > 1:
            try:
                if page.locator(FOUTMELDING).count():
                    return {}, "GEWEIGERD", verzonden
            except Exception:
                pass
        elif _host(page) == APP_HOST:
            break
        try:
            page.wait_for_timeout(500)
        except Exception:
            break
    if not gevangen and _host(page) == APP_HOST:
        try:
            vang_headers(page, HEADERS_WACHT_SEC, gevangen)
        except Exception:
            pass
    if gevangen:
        return dict(gevangen), "OK", verzonden
    return {}, "GEEN_API" if _host(page) == APP_HOST else "NIET_TERUG", verzonden


def herstel(page, moment):
    """Geen API-verkeer na het openen: de herstelvolgorde van de pc. Geeft (headers, route, code): route sso|wachtwoord|None,
    code = uitkomst van de wachtwoordpoging (parro_inlog.CODES) of GEEN_INLOGPAGINA / GEEN_WACHTWOORD."""
    soort = waarom_geen_headers(page)
    if soort == "geen_api":
        headers = klik_inloggen(page)
        if headers:
            return headers, "sso", "OK"
        soort = waarom_geen_headers(page)
    if soort != "uitgelogd":
        return {}, None, "GEEN_INLOGPAGINA"
    login = parro_inlog.lees()
    if not login:
        return {}, None, "GEEN_WACHTWOORD"
    headers, code, verzonden = probeer_inloggen(page, login, lambda: parro_inlog.voor_verzenden(login[2]))
    stand = parro_inlog.registreer(code, login[2], moment, verzonden)
    if code != "OK" and verzonden:
        tekst = None
        if code == "GEWEIGERD":
            tekst = "het bewaarde Parro-wachtwoord werd door ParnasSys geweigerd"
        elif stand and stand.get("gepauzeerd"):
            tekst = f"zelf inloggen bij Parro mislukte {parro_inlog.MAX_FOUTEN} keer op rij"
        elif stand and stand.get("budget_op"):
            tekst = f"zelf inloggen bij Parro mislukte {parro_inlog.BUDGET} keer in 24 uur (rem tegen accountblokkade)"
        if tekst:
            webhook.alarm("parro", "wachtwoord-fout", tekst + "; zelf inloggen staat uit tot het wachtwoord op de "
                          "inlogpagina van de meeleespod opnieuw is ingevuld (of na 24 uur, bij de rem)")
    return (headers, "wachtwoord", code) if headers else ({}, None, code)


def waarom_geen_headers(page):
    url = page.url
    if re.search(r"parnassys|inloggen|login|oauth|auth", url, re.I):
        return "uitgelogd"
    return "geen_api"


def _self_id(o):
    for l in o.get("links") or []:
        if l.get("rel") == "self" and l.get("id") is not None:
            return str(l["id"])
    for l in o.get("links") or []:
        if l.get("id") is not None:
            return str(l["id"])
    return None


def _persoon(o):
    o = o or {}
    return " ".join(x for x in (o.get("firstname"), o.get("surname")) if x).strip()


def lees_groepen(j):
    uit = []
    for g in j.get("items") or []:
        gid = _self_id(g)
        if gid and g.get("name"):
            uit.append({"id": gid, "naam": g["name"], "type": g.get("type", "")})
    return uit


def lees_mededelingen(j, groep):
    uit = []
    for e in j.get("items") or []:
        if e.get("deleted") or e.get("draft"):
            continue
        eid = _self_id(e)
        bijl = [a.get("name") or a.get("filename") or "" for a in (e.get("attachments") or []) if isinstance(a, dict)]
        uit.append({
            "id": f"mededeling:{eid}", "soort": "mededeling", "groep": groep,
            "titel": e.get("title") or "", "tekst": strip_html(e.get("contents") or ""),
            "afzender": _persoon(e.get("owner")), "datum": e.get("createdAt") or e.get("sortDate") or "",
            "bijlagen": [b for b in bijl if b], "gewijzigd": e.get("lastModifiedAt") or "",
        })
    return uit


def lees_agenda(j):
    uit = []
    for e in j.get("items") or []:
        if e.get("deleted") or e.get("cancelled"):
            continue
        ci = e.get("calendarItem") or {}
        if not ci.get("title"):
            continue
        if (e.get("dayNumber") or 1) != 1:
            continue
        namen = [g.get("name", "") for g in (e.get("groups") or []) if isinstance(g, dict) and g.get("name")]
        groepen = "hele school" if len(namen) > 8 else ", ".join(namen)
        start, eind = ci.get("startDate") or "", ci.get("endDate") or ""
        heledag = start.endswith("T00:00:00+02:00") or start.endswith("T00:00:00+01:00")
        tekst = (ci.get("description") or ci.get("contents") or "").strip()
        extra = []
        if ci.get("location"):
            extra.append("locatie: " + str(ci["location"]))
        if ci.get("rsvpRequired"):
            extra.append("aanmelden/rsvp vereist")
        if (e.get("numberOfDays") or 1) > 1:
            extra.append(f"{e['numberOfDays']} dagen, t/m {eind[:10]}")
        uit.append({
            "id": f"agenda:{_self_id(e) or _self_id(ci)}:{start[:10]}", "soort": "agenda",
            "groep": groepen or "school", "titel": ci["title"].strip(),
            "tekst": (("hele dag" if heledag else "om " + start[11:16]) + " — " + ci.get("type", "").lower() + (" — " + "; ".join(extra) if extra else "") + ("\n" + strip_html(tekst) if tekst else "")),
            "afzender": "schoolagenda", "datum": start, "einde": eind, "status": ci.get("status", ""),
            # pod: aanmaakmoment, alleen voor het zaaien (wordt niet meegestuurd)
            "aangemaakt": e.get("createdAt") or ci.get("createdAt") or "",
        })
    return uit


def lees_chats(j_rooms, haal_berichten):
    uit = []
    for r in j_rooms.get("items") or []:
        rid = _self_id(r)
        if not rid or r.get("archived"):
            continue
        naam = r.get("name") or ", ".join((r.get("memberNames") or [])[:4]) or r.get("type", "gesprek")
        if r.get("type") == "GROUP" and len(r.get("memberNames") or []) > 6:
            naam = f"groepschat ({len(r['memberNames'])} leden)"
        try:
            jm = haal_berichten(rid)
        except Exception as e:
            log("parro_chat_fout", code=type(e).__name__)
            continue
        for m in jm.get("items") or []:
            if m.get("deleted") or not m.get("text"):
                continue
            uit.append({
                "id": f"chat:{rid}:{_self_id(m) or m.get('createdAt')}", "soort": "chat", "groep": naam,
                "titel": "", "tekst": strip_html(m["text"]), "afzender": _persoon(m.get("identity")),
                "datum": m.get("createdAt") or "",
            })
    return uit


def haal_alles(api):
    """api(pad, range_hdr) -> json. Groepen, mededelingen per groep, schoolagenda en gesprekken.
    Geeft (items, groepen, volledig); volledig=False als een deel mislukte (dan niet zaaien, review MOET 4)."""
    groepen = lees_groepen(api("/group?dtype=identity.RHomeGroup", "items=0-99"))
    items, volledig = [], True
    for g in groepen:
        try:
            j = api(f"/event?dtype=event.RAnnouncementEventPrimer&group={g['id']}", "items=0-49")
            items.extend(lees_mededelingen(j, g["naam"]))
        except Exception as e:
            volledig = False
            log("parro_mededelingen_fout", code=type(e).__name__)
    try:
        sinds = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        j = api(f"/event?dtype=event.RCalendarItemEventPrimer&sort=desc-stream&sortDateSince={sinds}", "items=0-199")
        grens = (datetime.now() + timedelta(days=AGENDA_DAGEN_VOORUIT)).strftime("%Y-%m-%d")
        items.extend([a for a in lees_agenda(j) if a["datum"][:10] <= grens])
    except Exception as e:
        volledig = False
        log("parro_agenda_fout", code=type(e).__name__)
    try:
        jr = api("/chatroom", "items=0-99")
        items.extend(lees_chats(jr, lambda rid: api(f"/chatroom/{rid}/chatmessage", f"items=0-{CHAT_BERICHTEN - 1}")))
    except Exception as e:
        volledig = False
        log("parro_chats_fout", code=type(e).__name__)
    return dedupe(items), groepen, volledig


# ===== pod ===============================================================================================================
def _tijd(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def zaai(items, gezien, zaai_tot, nu_iso):
    """Eerste pod-ronde: alles van vóór de laatste pc-ronde geldt als al verstuurd (ontwerp § 8.2).
    Agenda-items zonder aanmaakmoment ook (hun 'datum' is de datum van het schoolevent, niet het moment van plaatsen)."""
    grens = _tijd(zaai_tot)
    n = 0
    for it in items:
        moment = _tijd(it.get("aangemaakt") or ("" if it["soort"] == "agenda" else it.get("datum")))
        if moment is None or grens is None or moment <= grens:
            if it["id"] not in gezien:
                gezien[it["id"]] = nu_iso
                n += 1
    return n


def kies_nieuw(items, gezien):
    grens = (datetime.now(timezone.utc) - timedelta(days=VENSTER_DAGEN)).isoformat()
    return [it for it in items if it["id"] not in gezien and (not it["datum"] or it["datum"] >= grens)]


def payload_items(nieuw):
    return [{k: v for k, v in it.items() if k not in INTERN} for it in nieuw]


def verwerk(items, groepen, st, stuur, volledig=True):
    """Kern van een ronde zonder browser (ook voor de droge toets). st = parro-staat (wordt aangepast).
    stuur(payload) -> antwoord-dict. Geeft een uitslag-dict met alleen aantallen.
    Zaaien alleen bij groepen én een volledige haal; anders wacht het (en wordt er ook nog niets verstuurd)."""
    nu = datetime.now(timezone.utc).isoformat()
    gezien = st.setdefault("gezien", {})
    uit = {"status": "ok", "gezien": len(items), "nieuw": 0, "groepen": len(groepen), "gezaaid": 0, "verstuurd": 0}
    if not groepen:
        uit["status"] = "geen_groepen"
        return uit
    if not st.get("gezaaid"):
        if not volledig:
            uit["status"] = "onvolledig_niet_gezaaid"
            return uit
        uit["gezaaid"] = zaai(items, gezien, I.PARRO_ZAAI_TOT, nu)
        st["gezaaid"] = nu
    nieuw = kies_nieuw(items, gezien)
    uit["nieuw"] = len(nieuw)
    if nieuw:
        antwoord = stuur({"items": payload_items(nieuw), "groepen": [g["naam"] for g in groepen]})
        if not antwoord.get("ok", True):
            raise RuntimeError("webhook gaf ok=false")
        for it in nieuw:
            gezien[it["id"]] = nu
        uit["verstuurd"] = len(nieuw)
    grens = (datetime.now(timezone.utc) - timedelta(days=STATE_BEWAAR_DAGEN)).isoformat()
    st["gezien"] = {k: v for k, v in gezien.items() if v >= grens}
    return uit


# ---- sessiemeting (wv1911) ----------------------------------------------------------------------------------------------
# Parro viel 28-10 ± 8 u na de inlog uit, terwijl stil vernieuwen tussendoor wél lukte. Twee verklaringen: de sessie is
# server-side begrensd, of het vernieuwde vernieuwtoken (rotatie) haalt de schijf niet omdat de browser direct na de
# API-aanroepen sluit, zodat elke ronde het token van de inlog opnieuw gebruikt tot dat verloopt. De meting bewaart
# alleen namen, tijden, statussen en een korte hash (8 hex van sha256) per vernieuwtoken: nooit een token of inhoud.
NA_VERNIEUW_WACHT_MS = 10000      # na een geziene vernieuwing de app de tijd geven om parro_db weg te schrijven
_NAAM = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
LEZENDE_METHODEN = ("GET", "HEAD", "OPTIONS")   # alleen kijken naar wat de app zelf schrijvend doet (de vernieuwing)
GOOGLE_HOST = re.compile(r"(^|\.)(googleapis\.com|google\.com|gstatic\.com|firebaseio\.com|firebase\.googleapis\.com|google-analytics\.com)$", re.I)
_TIJDNAAM = re.compile(r"(exp|valid|ttl|lifetime|until|tot)", re.I)


def _h8(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8] if isinstance(s, str) and s else None


def _namen(d):
    return sorted(k for k in d if isinstance(k, str) and _NAAM.match(k))[:20] if isinstance(d, dict) else []


def _vernieuwtoken(d):
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        if isinstance(k, str) and re.search(r"refresh", k, re.I) and isinstance(v, str) and len(v) >= 16:
            return v
    return None


def _jwt_tijden(tok):
    """exp/iat/auth_time/nbf van een JWT als ISO-tijd; {} als het geen JWT is."""
    try:
        deel = tok.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(deel + "=" * (-len(deel) % 4)))
    except Exception:
        return {}
    uit = {}
    for k in ("exp", "iat", "auth_time", "nbf"):
        if isinstance(payload.get(k), (int, float)):
            uit[k] = datetime.fromtimestamp(payload[k], timezone.utc).isoformat()
    return uit


def _getallen(d):
    """Numerieke velden met een tijd-achtige naam (expires_in, refresh_expires_in, expiresAt als getal)."""
    if not isinstance(d, dict):
        return {}
    return {k: d[k] for k in _namen(d) if _TIJDNAAM.search(k) and isinstance(d[k], (int, float))
            and not isinstance(d[k], bool)}


def _tijdteksten(d):
    """Tijd-achtige velden als ISO-tekst (expiresAt: "2026-…"); alleen als het op een datum lijkt."""
    if not isinstance(d, dict):
        return {}
    return {k: d[k][:32] for k in _namen(d) if _TIJDNAAM.search(k) and isinstance(d[k], str)
            and re.match(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:?\d{2})?$", d[k])}


def _pad(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    pad = (u.hostname or "") + re.sub(r"\d{2,}", "N", u.path)   # ids weg, v2 blijft
    return re.sub(r"[^A-Za-z0-9/._-]", "", pad)[:64]


def _lichaam(tekst):
    if not tekst:
        return {}
    try:
        j = json.loads(tekst)
        return j if isinstance(j, dict) else {}
    except ValueError:
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(tekst).items() if v}


def meet_vernieuwing(posts, vorige=None, nu=None):
    """posts = [(url, status, verzoektekst, antwoordtekst)] van POST's in deze ronde (zonder Google-hosts). Pure functie.
    vorige = de vorige meting (voor rotatie_bewaard). Geeft alleen namen, tijden, statussen en korte hashes."""
    uit = {"tijd": nu or datetime.now(timezone.utc).isoformat(), "posts": len(posts),
           "paden": sorted({_pad(p[0]) for p in posts})[:6], "vernieuwd": False}
    for post in posts:
        url, status, vt, at = post[:4]
        v, a = _lichaam(vt), _lichaam(at)
        rt_uit, rt_in = _vernieuwtoken(v), _vernieuwtoken(a)
        if not (rt_uit or rt_in or "refresh" in (vt or "").lower()):
            continue
        uit.update(vernieuwd=bool(rt_in), pad=_pad(url), http=status, verzoek_velden=_namen(v),
                   antwoord_velden=_namen(a), rt_uit=_h8(rt_uit), rt_in=_h8(rt_in),
                   antwoord_tijden={**_getallen(a), **_tijdteksten(a)}, rt_tijden=_jwt_tijden(rt_in or ""),
                   antwoord_lengte=len(at or ""))
        if len(post) > 4 and post[4]:
            uit["antwoord_soort"] = re.sub(r"[^a-z0-9/+.-]", "", str(post[4]).split(";")[0].strip().lower())[:40]
        fout = a.get("error") or a.get("code")
        if isinstance(fout, str) and re.match(r"^[A-Za-z_.-]{1,40}$", fout):
            uit["fout"] = fout
        break
    vorig_in = (vorige or {}).get("rt_in") or (vorige or {}).get("rt_vorige_in")
    if uit.get("rt_uit") and vorig_in:
        # ja = deze ronde stuurde het token dat de vorige ronde kreeg (rotatie bewaard); nee = een ouder token
        uit["rotatie_bewaard"] = uit["rt_uit"] == vorig_in
    if not uit.get("rt_in") and vorig_in:
        uit["rt_vorige_in"] = vorig_in
    return uit


def toegang_claims(headers):
    """Namen van de claims in het toegangstoken + auth_time en de host van iss (geen waarden)."""
    try:
        tok = headers.get("authorization", "").split(" ", 1)[1]
        deel = tok.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(deel + "=" * (-len(deel) % 4)))
    except Exception:
        return {}
    from urllib.parse import urlsplit
    uit = {"claims": _namen(payload)}
    if isinstance(payload.get("auth_time"), (int, float)):
        uit["auth_time"] = datetime.fromtimestamp(payload["auth_time"], timezone.utc).isoformat()
    if isinstance(payload.get("iss"), str):
        uit["iss_host"] = re.sub(r"[^a-z0-9.-]", "", (urlsplit(payload["iss"]).hostname or "").lower())[:64]
    return uit


class _PostVanger:
    """Verzamelt POST-antwoorden van Parro-hosts tijdens een ronde (lezen gebeurt pas na de ronde)."""

    def __init__(self, page):
        self.antwoorden = []
        page.on("response", self._op)

    def _op(self, r):
        try:
            from urllib.parse import urlsplit
            host = urlsplit(r.url).hostname or ""
            # alle POST's behalve Google/Firebase (firebaseinstallations heeft een eigen refreshToken): de vernieuwing
            # kan bij een aparte inloghost liggen; van niet-Parro-hosts komen ook alleen namen en hashes in de meting
            if r.request.method not in LEZENDE_METHODEN and not GOOGLE_HOST.search(host) and len(self.antwoorden) < 40:
                self.antwoorden.append(r)
        except Exception:
            pass

    def posts(self):
        uit = []
        for r in self.antwoorden:
            try:
                vt = r.request.post_data or ""
            except Exception:
                vt = ""
            try:
                at = r.text() if len(vt) < 100000 else ""
            except Exception:
                at = ""
            try:
                soort = r.headers.get("content-type", "")
            except Exception:
                soort = ""
            uit.append((r.url, r.status, vt, at[:200000], soort))
        return uit


def _open(pw, headless, profiel=None):
    """profiel=None: het echte Parro-profiel; anders een (proef)kopie."""
    d = vangst.dienstmap("parro")
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-sync"]
    if not headless:
        args += ["--kiosk", "--window-position=0,0", "--window-size=1280,900"]
    if profiel is None:
        vangst.ruim_profielslot("parro")   # wv1950: wees-Chromium/slot van een vorige pod weg; profiel blijft (BEHOUD_IN_PROFIEL)
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(profiel or d / "profiel"), headless=headless, viewport={"width": 1100, "height": 900} if headless else None,
            locale="nl-NL", args=args, accept_downloads=False)
    except Exception as e:
        raise vangst.BrowserStartFout(vangst.browser_subcode(str(e))) from None

    def bij_pagina(p):
        try:
            if p.opener() is None:
                return
            try:
                p.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            from urllib.parse import urlsplit
            host = urlsplit(p.url).hostname or ""
            if p.url not in ("about:blank", "") and not any(r.search(host) for r in POPUP_HOSTS):
                p.close()
        except Exception:
            pass
    ctx.on("page", bij_pagina)
    return ctx


def api_get(context, headers, pad, range_hdr="items=0-99"):
    url = API + pad
    host = vangnet.controleer_get(url, "parro")
    h = dict(headers)
    h["range"] = range_hdr
    h["topicus-Range-Count"] = "Exact"
    r = context.request.get(url, headers=h, timeout=30000, max_redirects=0)
    if r.status >= 300:
        raise vangnet.HttpFout(r.status, host)
    return r.json()


def inloggen_zichtbaar(max_s):
    """Het zichtbare inlogvenster voor Parro (aangeroepen door vangst.start_inlogvenster, slot is al genomen)."""
    from playwright.sync_api import sync_playwright
    vangst.zorg_voor_scherm()
    import os
    omg = vangst.kind_omgeving("parro")
    os.environ["DISPLAY"], os.environ["XAUTHORITY"] = omg["DISPLAY"], omg["XAUTHORITY"]
    with sync_playwright() as pw:
        ctx = _open(pw, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            vanger = _PostVanger(page)
            page.goto(APP_URL)
            headers = vang_headers(page, max_s)
            if headers and _bewaar_sessiemeting(vanger, headers, "inlog").get("vernieuwd"):
                page.wait_for_timeout(NA_VERNIEUW_WACHT_MS)
        finally:
            ctx.close()
    if headers:
        info = token_info(headers)
        return 0, {"ok": True, "code": "OK", "geldig_tot": info.get("exp")}
    return 4, {"ok": False, "code": "LOGIN_NODIG"}


def inloggen_stil():
    """Na een klik op Inloggen: eerst zonder venster kijken of het profiel nog binnenkomt (ook via 'Inloggen' = SSO)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _open(pw, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            vanger = _PostVanger(page)
            page.goto(APP_URL)
            headers = vang_headers(page, 45)
            if not headers:
                headers, _route, _code = herstel(page, "stil")
            if _bewaar_sessiemeting(vanger, headers or {}, "stil").get("vernieuwd"):
                page.wait_for_timeout(NA_VERNIEUW_WACHT_MS)
        finally:
            ctx.close()
    if headers:
        return 0, {"ok": True, "code": "OK", "geldig_tot": token_info(headers).get("exp")}
    return 4, {"ok": False, "code": "LOGIN_NODIG"}


def _bewaar_sessiemeting(vanger, headers, moment="ronde"):
    """Meting van deze ronde in /staat/parro_sessie.json (laatste 24) en terug voor de stand. Faalt nooit de ronde."""
    try:
        oud = staat.lees("parro_sessie")
        reeks = oud.get("reeks") or []
        m = meet_vernieuwing(vanger.posts(), reeks[-1] if reeks else None)
        m.update(toegang_claims(headers), moment=moment)
        staat.schrijf("parro_sessie", {"reeks": (reeks + [m])[-24:]})
        log("parro_sessie", status="vernieuwd" if m.get("vernieuwd") else "geen_vernieuwing", aantal=m.get("posts"),
            http=m.get("http") or 0, code=str(m.get("fout") or "-"),
            reden={True: "rotatie_bewaard", False: "rotatie_kwijt"}.get(m.get("rotatie_bewaard"), "onbekend"))
        return m
    except Exception as e:
        log("parro_sessie_fout", code=type(e).__name__)
        return {"fout_meting": type(e).__name__}


def ronde():
    """Eén Parro-ronde. Geeft een uitslag-dict (alleen aantallen en codes)."""
    from playwright.sync_api import sync_playwright
    if not vangst.browser_slot.acquire(timeout=600):
        return {"status": "bezig"}
    try:
        with sync_playwright() as pw:
            ctx = _open(pw, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                vanger = _PostVanger(page)
                page.goto(APP_URL)
                headers = vang_headers(page, HEADERS_WACHT_SEC)
                route = None
                if not headers:
                    headers, route, _code = herstel(page, "ronde")
                if not headers:
                    sessie = _bewaar_sessiemeting(vanger, {})
                    return {"status": "login_nodig", "sessie": sessie}
                info = token_info(headers)
                items, groepen, volledig = haal_alles(lambda pad, rng: api_get(ctx, headers, pad, rng))
                sessie = _bewaar_sessiemeting(vanger, headers)
                if sessie.get("vernieuwd"):
                    page.wait_for_timeout(NA_VERNIEUW_WACHT_MS)
            finally:
                ctx.close()
                vangst.wis_berichtcache("parro")
    finally:
        vangst.browser_slot.release()

    st = staat.lees("parro")
    try:
        uit = verwerk(items, groepen, st, lambda p: webhook.post("parro", p, timeout=WEBHOOK_TIMEOUT), volledig)
        st["fouten_op_rij"] = 0
    except Exception as e:
        # zoals de pc: na 3 keer op rij een alarm (review)
        st["fouten_op_rij"] = int(st.get("fouten_op_rij") or 0) + 1
        staat.schrijf("parro", st)
        log("parro_webhook_mislukt", code=type(e).__name__, fouten=st["fouten_op_rij"])
        if st["fouten_op_rij"] >= 3:
            webhook.alarm("parro", "webhook", f"Parro-webhook faalt {st['fouten_op_rij']}x op rij")
        return {"status": "webhook_fout", "code": type(e).__name__}
    staat.schrijf("parro", st)
    uit["geldig_tot"] = info.get("exp")
    uit["sessie"] = sessie
    if route:
        uit["herinlog"] = route   # sso | wachtwoord: deze ronde moest eerst opnieuw inloggen
    if uit["status"] == "geen_groepen":
        webhook.alarm("parro", "groepen", "geen groepen gevonden in het API-antwoord; uitlezer afstellen")
    elif uit["verstuurd"] == 0 and uit["status"] == "ok":
        webhook.levensteken("parro", {"gezien": uit["gezien"], "nieuw": 0, "groepen": uit["groepen"]})
    return uit


# ---- proef: gesimuleerd verlopen sessie (wv1951; pc: --test-login) -------------------------------------------------------
# Een kopie van het profiel ZONDER parro_db (IndexedDB), cookies en webopslag: de app is dan uitgelogd en de
# ParnasSys-SSO ook. Daarin de herstelvolgorde draaien. Het echte profiel blijft ongemoeid; de kopie gaat altijd weg.
PROEF_ZONDER = frozenset({"IndexedDB", "Local Storage", "Session Storage", "Sessions", "Cookies", "Cookies-journal",
                          "Service Worker", "Cache", "Code Cache", "GPUCache", "File System", "WebStorage", "blob_storage",
                          "shared_proto_db", "SingletonLock", "SingletonSocket", "SingletonCookie"})


def _proef_negeer(_map, namen):
    return [n for n in namen if n in PROEF_ZONDER]


_proef_slot = threading.Lock()
_proef_opnieuw = threading.Event()


def start_proef():
    """Proef op de achtergrond (na het bewaren of via /api/parro-proef); hooguit één tegelijk. Wordt er tijdens een
    lopende proef opnieuw gevraagd (nieuw wachtwoord), dan volgt er na afloop nog één (review ZOU 6). Lukt hij terwijl
    Parro uitgelogd staat, dan meteen een ronde: die logt dan in het echte profiel zelf opnieuw in."""
    if not _proef_slot.acquire(blocking=False):
        _proef_opnieuw.set()
        return True

    def loop():
        try:
            while True:
                _proef_opnieuw.clear()
                uit = proef_inlog()
                if not _proef_opnieuw.is_set():
                    break
            from . import planner
            if uit.get("code") == "OK" and planner.stand()["parro"].get("login_nodig"):
                planner.vraag_ronde("parro")
        except Exception as e:
            log("parro_proef_fout", code=type(e).__name__)
        finally:
            _proef_slot.release()
    try:
        threading.Thread(target=loop, daemon=True).start()
    except Exception:
        _proef_slot.release()
        raise
    return True


def _ruim_oude_proefkopieen(d):
    """Review KAN 10: een proef die door een podstop werd afgebroken laat een kopie (en misschien een Chromium) achter."""
    for oud in d.glob("proefprofiel-*"):
        for pid in vangst._profiel_pids(oud):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(oud, ignore_errors=True)


def proef_inlog():
    """Geeft {"code", "route"}; code OK alleen als de app na zelf inloggen eigen API-verkeer deed. Ook in parro_inlog
    (bij het wachtwoord waarmee geproefd is)."""
    from playwright.sync_api import sync_playwright
    login = parro_inlog.lees()
    if not login:
        uit = {"code": "GEEN_WACHTWOORD", "route": "geen"}
        parro_inlog.zet_proef(uit)
        return uit
    versie = login[2]
    if not vangst.browser_slot.acquire(timeout=600):
        uit = {"code": "BEZET", "route": "geen"}
        parro_inlog.zet_proef(uit, versie)
        return uit
    kopie = None
    uit = {"code": "FOUT", "route": "geen"}
    try:
        d = vangst.dienstmap("parro")
        _ruim_oude_proefkopieen(d)
        kopie = d / f"proefprofiel-{secrets.token_hex(4)}"
        if (d / "profiel").is_dir():
            shutil.copytree(d / "profiel", kopie, symlinks=True, ignore=_proef_negeer)
        else:
            kopie.mkdir(mode=0o700)
        with sync_playwright() as pw:
            ctx = _open(pw, headless=True, profiel=kopie)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(APP_URL)
                headers = vang_headers(page, KLIK_HEADERS_S)
                if headers:
                    uit = {"code": "SESSIE_NIET_WEG", "route": "geen"}   # de kopie was nog ingelogd: proef zegt niets
                else:
                    headers, route, code = herstel(page, "proef")
                    uit = {"code": "OK" if headers else code, "route": route or "geen"}
            finally:
                ctx.close()
    except vangst.BrowserStartFout as e:
        uit = {"code": "BROWSERSTART", "route": "geen", "sub": e.sub}
    except Exception as e:
        uit = {"code": "FOUT", "route": "geen"}
        log("parro_proef_fout", code=type(e).__name__)
    finally:
        if kopie is not None:
            for pid in vangst._profiel_pids(kopie):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            shutil.rmtree(kopie, ignore_errors=True)
        vangst.browser_slot.release()
    parro_inlog.zet_proef(uit, versie)
    log("parro_proef", code=uit["code"], stap=uit["route"])
    return uit
