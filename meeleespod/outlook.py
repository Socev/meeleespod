"""Outlook-lezer: eigen dunne leesclient op Outlook REST v2.0, alleen GET (ontwerp § 4).

Leest alleen /geheim/outlook/lezer.json (token, anchorMailbox, cookies van outlook.office.com) die de Node-vanger schreef.
Velden, mapuitsluiting, doorstuuralias en blokgrootte zijn die van outlook_meelezen.py (socev-pc 30524ca), zodat n8n
niets merkt behalve script_versie "pod-<commit>". Id = InternetMessageId zonder <>, zoals de pc (mail_bekend vangt de
overgang).
"""
import base64
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import bijlagen, instellingen as I
from . import staat, vangnet, webhook
from .logboek import ids_hash, log

BASIS = "https://outlook.office.com/api/v2.0/me"
BATCH = 4
WEBHOOK_TIMEOUT = 240
MAX_TEKST = 6000
STATE_BEWAAR_DAGEN = 45
OVERLAP = timedelta(hours=2)
MAX_PAGINAS = 40
MAX_MAPPEN = 500
MAX_MAPDIEPTE = 4
VENSTER_MIN = timedelta(days=2)   # altijd minstens 2 dagen terug (review: mail die later uit Ongewenst komt)
MAX_OPEENVOLGENDE_FOUTEN = 3
SB_ALIAS = "+sb@gmail.com"
UITGESLOTEN_ALIASSEN = ("JunkEmail", "DeletedItems", "Drafts", "Outbox", "ConversationHistory", "Archive",
                        "SyncIssues", "Conflicts", "LocalFailures", "ServerFailures", "Notes", "Scheduled",
                        "RecoverableItemsDeletions")
MAP_UITSLUITEN_NAAM = {"ongewenste e-mail", "junk email", "junk", "verwijderde items", "deleted items",
                       "concepten", "drafts", "postvak uit", "outbox", "gespreksgeschiedenis",
                       "conversation history", "archief", "archive", "rss-feeds", "rss feeds"}
SELECT = ("Id,InternetMessageId,ConversationId,ParentFolderId,From,Sender,ToRecipients,Subject,ReceivedDateTime,"
          "HasAttachments,IsDraft,Body,UniqueBody,BodyPreview")
TOEGESTANE_KLASSEN_BIJLAGEN = ("zakelijk", "twijfel")
# Bijlagen (review ZOU 1/2): n8n doet Qwen tot 3 × 240 s per stuk, plus Jev en de Intake → ruim wachten.
BIJLAGE_TIMEOUT = 960
HERKANS_MAX = 3          # herkansingen na de eerste poging; daarna opgeven (logregel bijlage_opgegeven)
HERKANS_PER_RONDE = 10
WACHTRIJ_MAX = 50


class LoginNodig(Exception):
    pass


def lees_sessie():
    p = I.GEHEIM / "outlook" / "lezer.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def geldig_s(sessie):
    try:
        return (datetime.fromisoformat(sessie["geldig_tot"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return -1


def _koppen(sessie, tekst=False):
    k = {"Authorization": "Bearer " + sessie["token"], "X-AnchorMailbox": sessie.get("anchorMailbox", ""),
         "Accept": "application/json"}
    if tekst:
        k["Prefer"] = 'outlook.body-content-type="text"'
    cookies = "; ".join(f"{c['name']}={c['value']}" for c in sessie.get("cookies") or [] if c.get("name"))
    if cookies:
        k["Cookie"] = cookies
    return k


def _qs(q):
    """OData-query: $-sleutels letterlijk, waarden met %-codering (spaties als %20)."""
    return "&".join(f"{k}={quote(str(v), safe=',:')}" for k, v in q.items())


def _get(sessie, url, tekst=False):
    try:
        return vangnet.get_json(url, koppen=_koppen(sessie, tekst), timeout=60, dienst="outlook")
    except vangnet.HttpFout as e:
        if e.status == 401:
            raise LoginNodig() from None
        raise


# ---- omzetten (uit outlook_meelezen.py) -------------------------------------------------------------------------------
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _adres(v):
    if isinstance(v, dict):
        ea = v.get("EmailAddress") or v.get("emailAddress") or {}
        return (ea.get("Name") or ea.get("name") or ""), (ea.get("Address") or ea.get("address") or "")
    return "", ""


def naam_email(v):
    if isinstance(v, list):
        delen = [_adres(x) for x in v]
        return ", ".join(n or e for n, e in delen if (n or e)), ", ".join(e for _, e in delen if e)
    return _adres(v)


def _tekst(b):
    if isinstance(b, dict):
        return strip_html(b.get("Content") or "")
    return ""


def _iso(v):
    """Zelfde vorm als de pc (datetime.isoformat in UTC: +00:00, geen Z)."""
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def naar_mail(m, mapnamen):
    """v2.0-bericht → het pc-formaat voor /webhook/outlook-classificatie. None = overslaan."""
    if m.get("IsDraft") is True:
        return None
    if "EventMessage" in str(m.get("@odata.type") or ""):
        return None   # uitnodigingen e.d. (de pc sloeg alles behalve IPM.Note over)
    mid = (m.get("InternetMessageId") or m.get("Id") or "").strip().strip("<>")
    if not mid:
        return None
    van_n, van_e = naam_email(m.get("From") or m.get("Sender"))
    aan_n, aan_e = naam_email(m.get("ToRecipients") or [])
    if SB_ALIAS and SB_ALIAS.lower() in (aan_n + " " + aan_e).lower():
        return None
    mapnaam = mapnamen.get(m.get("ParentFolderId") or "", "")
    tekst = _tekst(m.get("UniqueBody")) or _tekst(m.get("Body")) or (m.get("BodyPreview") or "")
    return {
        "id": mid[:300], "thread_id": (m.get("ConversationId") or "")[:300], "map": mapnaam,
        "van": van_n[:200], "van_email": van_e[:200], "aan": aan_n[:300],
        "onderwerp": (m.get("Subject") or "")[:300], "tekst": tekst[:MAX_TEKST],
        "ontvangen": _iso(m.get("ReceivedDateTime")), "bijlagen": [], "heeft_bijlagen": bool(m.get("HasAttachments")),
        "_v2id": m.get("Id"),
    }


# ---- mappen -----------------------------------------------------------------------------------------------------------
def mappen(sessie):
    """Geeft (mapnamen {id: naam}, uitgesloten {id}). Loopt ook submappen door (review: alleen het hoogste niveau
    miste 'Archief' e.d. als submap); een submap van een uitgesloten map is ook uitgesloten."""
    namen, uit = {}, set()
    for alias in UITGESLOTEN_ALIASSEN:
        try:
            j = _get(sessie, f"{BASIS}/MailFolders/{alias}?" + _qs({"$select": "Id,DisplayName"}))
            if j.get("Id"):
                uit.add(j["Id"])
                namen.setdefault(j["Id"], j.get("DisplayName") or alias)
        except vangnet.HttpFout:
            continue
    rij = [(f"{BASIS}/MailFolders?" + _qs({"$top": "250", "$select": "Id,DisplayName,ChildFolderCount"}), False, 0)]
    bekeken = 0
    while rij and bekeken < MAX_MAPPEN:
        url, ouder_uit, diepte = rij.pop(0)
        for _ in range(5):
            j = _get(sessie, url)
            for f in j.get("value") or []:
                fid = f.get("Id")
                bekeken += 1
                namen[fid] = f.get("DisplayName") or ""
                weg = ouder_uit or fid in uit or (f.get("DisplayName") or "").strip().lower() in MAP_UITSLUITEN_NAAM
                if weg:
                    uit.add(fid)
                if (f.get("ChildFolderCount") or 0) > 0 and diepte < MAX_MAPDIEPTE:
                    rij.append((f"{BASIS}/MailFolders/{quote(fid, safe='')}/childfolders?" +
                                _qs({"$top": "250", "$select": "Id,DisplayName,ChildFolderCount"}), weg, diepte + 1))
            url = j.get("@odata.nextLink")
            if not url:
                break
    return namen, uit


def berichten_sinds(sessie, sinds):
    q = {"$filter": f"ReceivedDateTime ge {sinds.strftime('%Y-%m-%dT%H:%M:%SZ')}", "$orderby": "ReceivedDateTime asc",
         "$top": "50", "$select": SELECT}
    url = f"{BASIS}/messages?" + _qs(q)
    uit = []
    for _ in range(MAX_PAGINAS):
        j = _get(sessie, url, tekst=True)
        uit.extend(j.get("value") or [])
        url = j.get("@odata.nextLink")
        if not url:
            break
    return uit


def bijlage_metadata(sessie, v2id):
    url = f"{BASIS}/messages/{quote(v2id, safe='')}/attachments?" + _qs({"$select": "Id,Name,ContentType,Size,IsInline,ContentId"})
    uit = []
    for a in _get(sessie, url).get("value") or []:
        ct = (a.get("ContentType") or "").split(";")[0].strip().lower()
        inline = bool(a.get("IsInline")) or (bool(a.get("ContentId")) and ct.startswith("image/"))   # zoals de pc
        uit.append({"naam": (a.get("Name") or "")[:200], "type": ct[:100],
                    "bytes": int(a.get("Size") or 0), "inline": inline, "id": (a.get("Id") or "")[:200],
                    "_soort": a.get("@odata.type") or ""})
    return uit


# ---- de ronde ---------------------------------------------------------------------------------------------------------
def ronde(modus="proef"):
    """modus proef = dry_run (n8n classificeert, bewaart niets, gezien blijft gelijk); live = zoals de pc."""
    sessie = lees_sessie()
    if not sessie:
        return {"status": "niet_ingelogd"}
    st = staat.lees("outlook")
    gezien = st.setdefault("gezien", {})
    oordelen = staat.lees("oordelen")
    nu = datetime.now(timezone.utc)
    try:
        wl = datetime.fromisoformat(st["waterlijn"]) if st.get("waterlijn") else None
    except ValueError:
        wl = None
    sinds = min(wl - OVERLAP, nu - VENSTER_MIN) if wl else nu - timedelta(days=I.EERSTE_VENSTER_DAGEN)
    try:
        namen, uitgesloten = mappen(sessie)
        ruw = berichten_sinds(sessie, sinds)
    except LoginNodig:
        return {"status": "login_nodig"}
    mails, n_uit, nieuwste = [], 0, wl
    for m in ruw:
        t = m.get("ReceivedDateTime")
        if m.get("ParentFolderId") in uitgesloten:
            n_uit += 1
            continue
        mail = naar_mail(m, namen)
        if mail is None:
            n_uit += 1
            continue
        if mail["id"] in gezien:
            continue
        mails.append(mail)
    # bijlage-namen (alleen metadata, GET) voor mails met bijlagen, zoals de pc ze uit de cache haalde
    for mail in mails:
        if mail["heeft_bijlagen"] and mail.get("_v2id"):
            try:
                mail["_bijlagen_meta"] = bijlage_metadata(sessie, mail["_v2id"])
                mail["bijlagen"] = [{k: v for k, v in b.items() if not k.startswith("_")} for b in mail["_bijlagen_meta"]]
            except LoginNodig:
                return {"status": "login_nodig"}
            except vangnet.HttpFout:
                pass
    dry = modus != "live"
    tel, fouten_op_rij, verstuurd, bijl_tel = {}, 0, 0, {"doorgelaten": 0, "geweigerd": 0, "herkansing": 0, "opgegeven": 0}
    ooit_fout = False   # review MOET 2: één mislukt blok = waterlijn blijft staan (gezien voorkomt dubbel versturen)
    if not dry and I.BIJLAGEN_AAN:
        # eerst wat bij een eerdere ronde door een storing bleef liggen (niet wat deze ronde zelf in de wachtrij zet)
        for k, v in herkans_bijlagen(sessie, oordelen).items():
            bijl_tel[k] = bijl_tel.get(k, 0) + v
    for i in range(0, len(mails), BATCH):
        blok = mails[i:i + BATCH]
        payload = [{k: v for k, v in m.items() if not k.startswith("_")} for m in blok]
        try:
            data = webhook.post("outlook", {"dry_run": dry, "mails": payload}, timeout=WEBHOOK_TIMEOUT)
            if not isinstance(data, dict) or not data.get("ok"):
                raise RuntimeError("ok=false")
        except Exception as e:
            fouten_op_rij += 1
            ooit_fout = True
            log("outlook_blok_mislukt", code=type(e).__name__, aantal=len(blok))
            if fouten_op_rij >= MAX_OPEENVOLGENDE_FOUTEN:
                webhook.alarm("outlook", "webhook", "outlook-classificatie-webhook faalt herhaaldelijk")
                break
            continue
        fouten_op_rij = 0
        verstuurd += len(blok)
        per_id = {r.get("id"): r for r in data.get("results") or [] if isinstance(r, dict)}
        for m in blok:
            r = per_id.get(m["id"])
            klasse = r.get("klasse") if isinstance(r, dict) and isinstance(r.get("klasse"), str) else None
            # B3-review MOET 1: n8n zet terugval: true als Qwen geen bruikbaar oordeel gaf en n8n zelf 'twijfel' koos.
            # Dat is geen uitdrukkelijk oordeel: niet bewaren, dus nooit bijlagen (ook niet als het veld raar gevuld is).
            terugval = isinstance(r, dict) and r.get("terugval") not in (None, False)
            sleutel = (klasse or "geen_oordeel") if not terugval else "terugval"
            tel[sleutel] = tel.get(sleutel, 0) + 1
            if not dry:
                # alleen een oordeel uit een live-ronde telt; ontbreekt het, dan weg met een oud oordeel (review)
                if klasse and not terugval:
                    oordelen[m["id"]] = {"klasse": klasse, "tijd": nu.isoformat()}   # alleen de klasse, geen tekst
                else:
                    oordelen.pop(m["id"], None)
                gezien[m["id"]] = nu.isoformat()
                if I.BIJLAGEN_AAN:
                    for k, v in bijlagen_voor_mail(sessie, m, oordelen).items():
                        bijl_tel[k] = bijl_tel.get(k, 0) + v
        if not dry:
            staat.schrijf("outlook", st)
    for m in ruw:
        try:
            t = datetime.fromisoformat((m.get("ReceivedDateTime") or "").replace("Z", "+00:00"))
            nieuwste = t if nieuwste is None or t > nieuwste else nieuwste
        except ValueError:
            pass
    if not dry and not ooit_fout and nieuwste:
        st["waterlijn"] = nieuwste.isoformat()
    grens = (nu - timedelta(days=STATE_BEWAAR_DAGEN)).isoformat()
    st["gezien"] = {k: v for k, v in gezien.items() if v >= grens}
    staat.schrijf("outlook", st)
    staat.schrijf("oordelen", {k: v for k, v in oordelen.items() if v.get("tijd", "") >= grens})
    uit = {"status": "ok" if not ooit_fout else "webhook_fout", "modus": modus, "gezien": len(ruw), "nieuw": len(mails),
           "verstuurd": verstuurd, "overig": n_uit, "ids": ids_hash(m["id"] for m in mails), **tel}
    if I.BIJLAGEN_AAN and not dry:
        uit.update(bijl_tel)
        uit["wachtrij"] = len(staat.lees("bijlage_wachtrij").get("rij") or [])
    if verstuurd == 0 and not ooit_fout and not dry:
        try:
            webhook.levensteken("outlook")
        except Exception:
            pass
    return uit


def _reden_code(r):
    """n8n-reden als korte code voor het log (alleen het eerste woord, bv. 'patientgebonden (BSN-regel)' → patientgebonden)."""
    m = re.match(r"[a-z_]{1,32}", r if isinstance(r, str) else "")
    return m.group(0) if m else "onbekend"


def lever_bijlage(sessie, mail_id, thread_id, v2id, bijlage_id, naam=""):
    """Haalt één bijlage op, beoordeelt hem en stuurt hem naar n8n. Geeft door | geweigerd | herkansen.
    Herkansen alleen bij een storing (review ZOU 1): n8n 503 (Qwen), een andere 5xx (bv. Intake legde niet vast),
    time-out of verbindingsfout, of Outlook tijdelijk niet bereikbaar. Een weigering (200 door:false, 4xx) is definitief."""
    url = f"{BASIS}/messages/{quote(v2id, safe='')}/attachments/{quote(bijlage_id, safe='')}"
    try:
        j = _get(sessie, url)
    except (LoginNodig, vangnet.VerbindFout):
        return "herkansen"
    except vangnet.HttpFout as e:
        return "herkansen" if e.status >= 500 else "geweigerd"
    except Exception:
        return "geweigerd"
    if not isinstance(j, dict) or "FileAttachment" not in str(j.get("@odata.type") or "FileAttachment") or j.get("IsInline") is True:
        return "geweigerd"
    try:
        inhoud = base64.b64decode(j.get("ContentBytes") or "", validate=True)
    except ValueError:
        return "geweigerd"
    naam = (j.get("Name") or naam or "")[:200]
    j = None
    besluit = bijlagen.beoordeel(naam, inhoud)
    if not besluit["door"]:
        log("bijlage_geweigerd", dienst="outlook", reden=besluit["reden"])
        return "geweigerd"
    try:
        data = webhook.post("outlook-bijlage", {
            "mail_id": mail_id, "thread_id": thread_id, "naam": naam, "type": besluit["soort"],
            "tekst": besluit["tekst"], "bytes_b64": base64.b64encode(inhoud).decode("ascii"),
            "sha256": besluit["sha256"]}, timeout=BIJLAGE_TIMEOUT)
    except vangnet.HttpFout as e:
        log("bijlage_niet_afgeleverd", dienst="outlook", http=e.status)
        return "herkansen" if e.status >= 500 else "geweigerd"
    except vangnet.VerbindFout as e:
        log("bijlage_niet_afgeleverd", dienst="outlook", code=e.soort)
        return "herkansen"
    except Exception as e:
        log("bijlage_niet_afgeleverd", dienst="outlook", code=type(e).__name__)
        return "geweigerd"
    finally:
        inhoud = None
    # review ZOU 2: alleen door:true (en ok) is doorgelaten; door:false is een weigering door n8n, geen aflevering
    if isinstance(data, dict) and data.get("ok") is True and data.get("door") is True:
        log("bijlage_doorgelaten", dienst="outlook", status="stuk" if data.get("stuk_id") else "zonder_stuk_id")
        return "door"
    log("bijlage_geweigerd", dienst="outlook", reden="n8n_" + _reden_code(data.get("reden") if isinstance(data, dict) else ""))
    return "geweigerd"


def _in_wachtrij(mail, b):
    """Alleen ids (mail, gesprek, bericht, bijlage) en een teller; nooit inhoud of naam (review ZOU 1)."""
    def f(d):
        rij = d.setdefault("rij", [])
        if any(x.get("mail_id") == mail["id"] and x.get("bijlage_id") == b["id"] for x in rij):
            return False
        if len(rij) >= WACHTRIJ_MAX:
            log("bijlage_opgegeven", dienst="outlook", reden="wachtrij_vol")
            return False
        rij.append({"mail_id": mail["id"], "thread_id": mail.get("thread_id") or "", "v2id": mail["_v2id"],
                    "bijlage_id": b["id"], "pogingen": 1, "sinds": datetime.now(timezone.utc).isoformat()})
        return True
    return staat.werk_bij("bijlage_wachtrij", f)


def bijlagen_voor_mail(sessie, mail, oordelen):
    """Ontwerp § 5.3: alleen bij een UITDRUKKELIJK oordeel zakelijk of twijfel (geen terugval); alleen bestanden met een
    echte tekstlaag. Geeft tellers {doorgelaten, geweigerd, herkansing}."""
    tel = {"doorgelaten": 0, "geweigerd": 0, "herkansing": 0}
    oordeel = oordelen.get(mail["id"]) if isinstance(oordelen.get(mail["id"]), dict) else None
    if not bijlagen.mag_bijlagen(oordeel):
        return tel
    meta = [b for b in mail.get("_bijlagen_meta") or [] if not b.get("inline") and "FileAttachment" in b.get("_soort", "")]
    for b in meta[:bijlagen.MAX_PER_MAIL]:
        if b.get("bytes", 0) > bijlagen.MAX_BYTES or not b.get("id"):
            tel["geweigerd"] += 1
            continue
        uit = lever_bijlage(sessie, mail["id"], mail.get("thread_id") or "", mail["_v2id"], b["id"], b.get("naam") or "")
        if uit == "door":
            tel["doorgelaten"] += 1
        elif uit == "herkansen" and _in_wachtrij(mail, b):
            tel["herkansing"] += 1
        elif uit == "herkansen":
            tel["geweigerd"] += 1   # al in de wachtrij of wachtrij vol
        else:
            tel["geweigerd"] += 1
    return tel


def herkans_bijlagen(sessie, oordelen):
    """Bij de volgende live-ronde: wat door een storing bleef liggen opnieuw ophalen en sturen; hooguit HERKANS_MAX
    herkansingen per bijlage, dan opgeven met een logregel. Het oordeel moet dan nog steeds uitdrukkelijk zijn."""
    tel = {"doorgelaten": 0, "geweigerd": 0, "herkansing": 0, "opgegeven": 0}
    rij = list((staat.lees("bijlage_wachtrij").get("rij") or []))
    if not rij:
        return tel
    blijft = []
    for x in rij[:HERKANS_PER_RONDE]:
        oordeel = oordelen.get(x.get("mail_id")) if isinstance(oordelen.get(x.get("mail_id")), dict) else None
        if not bijlagen.mag_bijlagen(oordeel) or not all(isinstance(x.get(k), str) and x.get(k) for k in ("mail_id", "v2id", "bijlage_id")):
            tel["geweigerd"] += 1
            continue
        uit = lever_bijlage(sessie, x["mail_id"], x.get("thread_id") or "", x["v2id"], x["bijlage_id"])
        if uit == "door":
            tel["doorgelaten"] += 1
        elif uit == "geweigerd":
            tel["geweigerd"] += 1
        else:
            x["pogingen"] = int(x.get("pogingen") or 1) + 1
            if x["pogingen"] > HERKANS_MAX + 1:
                tel["opgegeven"] += 1
                log("bijlage_opgegeven", dienst="outlook", poging=x["pogingen"] - 1)
            else:
                tel["herkansing"] += 1
                blijft.append(x)
    blijft += rij[HERKANS_PER_RONDE:]
    klaar = {(x.get("mail_id"), x.get("bijlage_id")) for x in rij}

    def f(d):
        # wat in de tussentijd door een andere aanroep is bijgezet, blijft staan
        nieuw = [y for y in d.get("rij") or [] if (y.get("mail_id"), y.get("bijlage_id")) not in klaar]
        d["rij"] = blijft + nieuw
    staat.werk_bij("bijlage_wachtrij", f)
    return tel


def vernieuw_nodig():
    s = lees_sessie()
    return s is not None and geldig_s(s) < I.VERNIEUW_ONDER_S
