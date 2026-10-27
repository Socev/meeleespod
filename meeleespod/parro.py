"""Parro-lezer: parro_meelezen.py uit Socev/socev-pc (commit 30524ca), met alleen de pc-plekken anders.

Wat anders is dan op de pc (ontwerp § 4, parro-lezer):
- socev_pc.py → meeleespod: datamap = /staat (gezien-lijst) en /geheim/parro (browserprofiel), sleutel via /api/koppel,
  versie = image-commit.
- Geen opgeslagen wachtwoord (de keyring-route vervalt): verlopen = één alarm + inloggen via de inlogpagina.
- Geen HTML-overzicht, geen schermafdruk, geen ontdek-map: er komt geen Parro-tekst op schijf.
- Alle API-aanroepen gaan eerst langs het vangnet (alleen GET naar rest-v2.parro.com, geen redirects).
- Zaaien (ontwerp § 8.2): de eerste ronde zet alles van vóór de laatste pc-ronde op gezien zonder te versturen.
De leeslogica (groepen, mededelingen, agenda, gesprekken, ids) is ongewijzigd, zodat de ids gelijk zijn aan de pc.
"""
import base64
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

from . import instellingen as I
from . import staat, vangnet, vangst, webhook
from .logboek import log

APP_URL = "https://talk.parro.com/"
API = "https://rest-v2.parro.com/rest/v2"
VENSTER_DAGEN = 21
WEBHOOK_TIMEOUT = 300
STATE_BEWAAR_DAGEN = 120
AGENDA_DAGEN_VOORUIT = 120
CHAT_BERICHTEN = 30
HEADERS_WACHT_SEC = 90
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


def vang_headers(page, seconden=45):
    gevangen = {}

    def on_request(req):
        if "rest-v2.parro.com" in req.url and req.headers.get("authorization") and not gevangen:
            for k in ("authorization", "accept", "parro-authorization-role", "parro-app-version", "content-type"):
                if req.headers.get(k):
                    gevangen[k] = req.headers[k]
    page.on("request", on_request)
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
    """Welkomstscherm van de app: 'Inloggen' (Flutter-canvas, vaste positie bij viewport 1100x900)."""
    try:
        page.mouse.click(550, 570)
        page.wait_for_timeout(4000)
    except Exception:
        return {}
    return vang_headers(page, 45)


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
    """api(pad, range_hdr) -> json. Groepen, mededelingen per groep, schoolagenda en gesprekken. Geeft (items, groepen)."""
    groepen = lees_groepen(api("/group?dtype=identity.RHomeGroup", "items=0-99"))
    items = []
    for g in groepen:
        try:
            j = api(f"/event?dtype=event.RAnnouncementEventPrimer&group={g['id']}", "items=0-49")
            items.extend(lees_mededelingen(j, g["naam"]))
        except Exception as e:
            log("parro_mededelingen_fout", code=type(e).__name__)
    try:
        sinds = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        j = api(f"/event?dtype=event.RCalendarItemEventPrimer&sort=desc-stream&sortDateSince={sinds}", "items=0-199")
        grens = (datetime.now() + timedelta(days=AGENDA_DAGEN_VOORUIT)).strftime("%Y-%m-%d")
        items.extend([a for a in lees_agenda(j) if a["datum"][:10] <= grens])
    except Exception as e:
        log("parro_agenda_fout", code=type(e).__name__)
    try:
        jr = api("/chatroom", "items=0-99")
        items.extend(lees_chats(jr, lambda rid: api(f"/chatroom/{rid}/chatmessage", f"items=0-{CHAT_BERICHTEN - 1}")))
    except Exception as e:
        log("parro_chats_fout", code=type(e).__name__)
    return dedupe(items), groepen


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


def verwerk(items, groepen, st, stuur):
    """Kern van een ronde zonder browser (ook voor de droge toets). st = parro-staat (wordt aangepast).
    stuur(payload) -> antwoord-dict. Geeft een uitslag-dict met alleen aantallen."""
    nu = datetime.now(timezone.utc).isoformat()
    gezien = st.setdefault("gezien", {})
    gezaaid = 0
    if not st.get("gezaaid"):
        gezaaid = zaai(items, gezien, I.PARRO_ZAAI_TOT, nu)
        st["gezaaid"] = nu
    nieuw = kies_nieuw(items, gezien)
    uit = {"status": "ok", "gezien": len(items), "nieuw": len(nieuw), "groepen": len(groepen), "gezaaid": gezaaid,
           "verstuurd": 0}
    if not groepen:
        uit["status"] = "geen_groepen"
        return uit
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


def _open(pw, headless):
    d = vangst.dienstmap("parro")
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-sync"]
    if not headless:
        args += ["--kiosk", "--window-position=0,0", "--window-size=1280,900"]
    ctx = pw.chromium.launch_persistent_context(
        str(d / "profiel"), headless=headless, viewport={"width": 1100, "height": 900} if headless else None,
        locale="nl-NL", args=args, accept_downloads=False)

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
            page.goto(APP_URL)
            headers = vang_headers(page, max_s)
        finally:
            ctx.close()
    if headers:
        info = token_info(headers)
        return 0, {"ok": True, "code": "OK", "geldig_tot": info.get("exp")}
    return 4, {"ok": False, "code": "LOGIN_NODIG"}


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
                page.goto(APP_URL)
                headers = vang_headers(page, HEADERS_WACHT_SEC)
                if not headers and waarom_geen_headers(page) == "geen_api":
                    headers = klik_inloggen(page)
                if not headers:
                    return {"status": "login_nodig"}
                info = token_info(headers)
                items, groepen = haal_alles(lambda pad, rng: api_get(ctx, headers, pad, rng))
            finally:
                ctx.close()
    finally:
        vangst.browser_slot.release()

    st = staat.lees("parro")
    uit = verwerk(items, groepen, st, lambda p: webhook.post("parro", p, timeout=WEBHOOK_TIMEOUT))
    staat.schrijf("parro", st)
    uit["geldig_tot"] = info.get("exp")
    if uit["status"] == "geen_groepen":
        webhook.alarm("parro", "groepen", "geen groepen gevonden in het API-antwoord; uitlezer afstellen")
    elif uit["verstuurd"] == 0:
        webhook.levensteken("parro", {"gezien": uit["gezien"], "nieuw": 0, "groepen": uit["groepen"]})
    return uit
