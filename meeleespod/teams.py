"""Teams-lezer: eigen dunne GET-client (ontwerp § 4, review M2: de pakketklassen dragen ook post/patch/delete en komen
daarom niet in de bundel). Paden en koppen overgenomen uit teams-access 1aa084d:
- chatlijst: GET teams.microsoft.com/api/csa/<regio>/api/v1/teams/users/me/updates?isPrefetch=false  (chatsvcagg-token)
- berichten: GET teams.microsoft.com/api/chatsvc/<regio>/v1/users/ME/conversations/<id>/messages?...  (ic3-token)
- kanaalposts: GET teams.microsoft.com/api/csa/<regio>/api/v1/containers/<kanaal>/posts?modality=post&teamId=<algemeen>
"Sinds" filtert de lezer zelf (ook voor kanalen); vaste pauze tussen aanroepen, bij 429/503 één keer wachten.
Payload, blokgrootte (8), id-vorm <bericht>@<gesprek> en systeemfilter zijn die van teams_meelezen.py (socev-pc 30524ca).
"""
import hashlib
import html
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import instellingen as I
from . import staat, vangnet, webhook
from .logboek import ids_hash, log

HOST = "https://teams.microsoft.com"
CLIENT_VERSION = "1415/25021922252"   # zelfde waarde als teams-access (chatsvc-client.ts / chatsvcagg-client.ts)
BATCH = 8
WEBHOOK_TIMEOUT = 240
MAX_TEKST = 4000
STATE_BEWAAR_DAGEN = 45
OVERLAP_MS = 5 * 60 * 1000
PAUZE_S = 0.4
MAX_PAGINAS_PER_CHAT = 5
MAX_OPEENVOLGENDE_FOUTEN = 3
BLACKLIST = {
    "19:d3f8d682-758e-46f9-bd04-16a695ca453a_817c2506-de4a-4795-971e-371ea75a03ed@unq.gbl.spaces",   # Polly-bot
    "48:calllogs", "48:notifications", "48:annotations",
}


class LoginNodig(Exception):
    pass


def lees_sessie():
    try:
        return json.loads((I.GEHEIM / "teams" / "lezer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def geldig_s(sessie):
    exps = [t.get("exp") for n, t in (sessie.get("tokens") or {}).items() if n in ("chatsvcagg", "ic3") and t.get("exp")]
    if len(exps) < 2:
        return -1
    return min(exps) - time.time()


def vernieuw_nodig():
    s = lees_sessie()
    return s is not None and geldig_s(s) < I.VERNIEUW_ONDER_S


def _get(sessie, aud, url):
    tok = ((sessie.get("tokens") or {}).get(aud) or {}).get("token")
    if not tok:
        raise LoginNodig()
    koppen = {"Authorization": "Bearer " + tok, "Accept": "application/json",
              "x-ms-client-version": CLIENT_VERSION, "x-ms-session-id": str(uuid.uuid4())}
    for poging in range(2):
        try:
            time.sleep(PAUZE_S)
            return vangnet.get_json(url, koppen=koppen, timeout=90, dienst="teams")
        except vangnet.HttpFout as e:
            if e.status == 401:
                raise LoginNodig() from None
            if e.status in (429, 503) and poging == 0:
                time.sleep(30)
                continue
            raise


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def is_systeembericht(content, msg):
    mt = str(msg.get("messagetype") or msg.get("messageType") or msg.get("type") or "").lower()
    if any(x in mt for x in ("threadactivity", "control", "eventmessage")):
        return True
    c = (content or "").strip()
    if not c:
        return True
    if c.startswith("{") and ("eventtime" in c or "oldvalue" in c.lower() or "members" in c):
        return True
    if re.match(r"^\d{12,}\s+\d*:?orgid:", c) or re.match(r"^\d{12,}\s+8:", c):
        return True
    if c.startswith("Call Logs for Call"):
        return True
    if re.search(r"\b(callStarted|callEnded)\b", c) or re.search(r"\b8:(orgid|live|teamsvisitor):", c):
        return True
    if "event/call" in mt or "richtext/media_card" in mt:
        return True
    return False


def _ms(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).strip().isdigit():
            n = float(v)
            return int(n if n > 1e11 else n * 1000)
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except ValueError:
        return None


def sleutel(mid, cid):
    s = f"{mid}@{cid}"
    if len(s) > 200:
        s = mid + "@" + hashlib.sha1(cid.encode("utf-8")).hexdigest()[:24]
    return s


def naar_bericht(m, cid, naam):
    inhoud = strip_html(m.get("content") or "")
    if is_systeembericht(inhoud, m):
        return None
    t = _ms(m.get("originalarrivaltime") or m.get("composetime") or m.get("clientArrivalTime"))
    mid = str(m.get("id") or m.get("clientmessageid") or "")
    if not mid:
        mid = hashlib.sha1((cid + "|" + str(t or "") + "|" + inhoud).encode("utf-8")).hexdigest()
    return {"id": sleutel(mid, cid), "conversation_id": cid, "gesprek_naam": naam,
            "afzender": str(m.get("imdisplayname") or m.get("imDisplayName") or ""), "tekst": inhoud[:MAX_TEKST],
            "tijd": datetime.fromtimestamp(t / 1000, timezone.utc).isoformat() if t else None, "_ms": t}


def chatnaam(c):
    if c.get("topic") or c.get("title"):
        return str(c.get("topic") or c.get("title"))
    namen = sorted({str(m.get("displayName")) for m in c.get("members") or [] if isinstance(m, dict) and m.get("displayName")})
    return ("chat: " + ", ".join(namen[:3]) + (", ..." if len(namen) > 3 else "")) if namen else ""


def kanalen(raw):
    """Uit het updates-antwoord: (kanaal-id, algemeen-id, naam, laatste_ms). Algemeen-id = de teamId die csa wil."""
    for t in raw.get("teams") or []:
        if not isinstance(t, dict):
            continue
        chans = [c for c in t.get("channels") or [] if isinstance(c, dict) and c.get("id")]
        algemeen = next((c["id"] for c in chans if c.get("isGeneral")), None) \
            or next((c["id"] for c in chans if str(c.get("displayName", "")).lower() in ("general", "algemeen")), None) \
            or (t.get("id") if str(t.get("id", "")).startswith("19:") else None)
        if not algemeen:
            continue
        for c in chans:
            lm = c.get("lastMessage") if isinstance(c.get("lastMessage"), dict) else {}
            yield c["id"], algemeen, str(c.get("displayName") or t.get("displayName") or ""), _ms(lm.get("composeTime"))


def verzamel(sessie, st):
    """Haalt nieuwe berichten op. Geeft (berichten, nieuwe_waterlijnen, tellers)."""
    regio = sessie.get("region") or {}
    csa, chatsvc = regio.get("csa") or "emea", regio.get("chatsvc") or "emea"
    wl = st.setdefault("wl", {})
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=I.EERSTE_VENSTER_DAGEN)).timestamp() * 1000)
    raw = _get(sessie, "chatsvcagg", f"{HOST}/api/csa/{quote(csa)}/api/v1/teams/users/me/updates?isPrefetch=false")
    berichten, nieuw_wl, tel = [], {}, {"chats": 0, "kanalen": 0, "fouten": 0, "401": 0, "goed": 0}
    for c in raw.get("chats") or []:
        cid = str(c.get("id") or "")
        if not cid or cid in BLACKLIST or cid.startswith("48:"):
            continue
        sinds = wl.get(cid, start_ms)
        lm = c.get("lastMessage") if isinstance(c.get("lastMessage"), dict) else {}
        laatste = _ms(lm.get("composeTime"))
        if laatste is not None and laatste <= sinds:
            continue
        tel["chats"] += 1
        naam = chatnaam(c)
        try:
            url = (f"{HOST}/api/chatsvc/{quote(chatsvc)}/v1/users/ME/conversations/{quote(cid, safe='')}/messages?"
                   f"view={quote('msnp24Equivalent|supportsMessageProperties')}&pageSize=50&startTime={max(1, sinds - OVERLAP_MS)}")
            hoogste = sinds
            for _ in range(MAX_PAGINAS_PER_CHAT):
                j = _get(sessie, "ic3", url)
                msgs = j.get("messages") or []
                for m in msgs:
                    b = naar_bericht(m, cid, naam)
                    if b and b["_ms"] and b["_ms"] > sinds - OVERLAP_MS:
                        berichten.append(b)
                        hoogste = max(hoogste, b["_ms"])
                oud = min((_ms(m.get("originalarrivaltime") or m.get("composetime")) or 0 for m in msgs), default=0)
                link = (j.get("_metadata") or {}).get("backwardLink")
                if not msgs or not link or oud <= sinds:
                    break
                url = link   # vangnet toetst de host (alleen teams.microsoft.com)
            nieuw_wl[cid] = hoogste
            tel["goed"] += 1
        except LoginNodig:
            tel["401"] += 1   # één chat met 401 is nog geen uitgelogd account (review); zie onder
            tel["fouten"] += 1
        except Exception as e:
            tel["fouten"] += 1
            log("teams_chat_fout", code=type(e).__name__, http=getattr(e, "status", 0))
    for kid, algemeen, naam, laatste in kanalen(raw):
        sinds = wl.get(kid, start_ms)
        if laatste is not None and laatste <= sinds:
            continue
        tel["kanalen"] += 1
        try:
            url = (f"{HOST}/api/csa/{quote(csa)}/api/v1/containers/{quote(kid, safe='')}/posts?modality=post&pageSize=20"
                   f"&teamId={quote(algemeen, safe='')}")
            j = _get(sessie, "chatsvcagg", url)
            hoogste = sinds
            for p in j.get("posts") or []:
                m = dict(p.get("message") or {})
                m.setdefault("id", p.get("id"))
                if not (m.get("composetime") or m.get("originalarrivaltime")):
                    m["composetime"] = p.get("latestMessageTime")
                b = naar_bericht(m, kid, naam)
                if b and b["_ms"] and b["_ms"] > sinds - OVERLAP_MS:
                    berichten.append(b)
                    hoogste = max(hoogste, b["_ms"])
            nieuw_wl[kid] = hoogste
            tel["goed"] += 1
        except LoginNodig:
            tel["401"] += 1
            tel["fouten"] += 1
        except Exception as e:
            tel["fouten"] += 1
            log("teams_kanaal_fout", code=type(e).__name__, http=getattr(e, "status", 0))
    if tel["401"] and not tel["goed"]:
        raise LoginNodig()   # alles 401: dan is de inlog echt weg
    return berichten, nieuw_wl, tel


def ronde(modus="proef"):
    sessie = lees_sessie()
    if not sessie:
        return {"status": "niet_ingelogd"}
    st = staat.lees("teams")
    gezien = st.setdefault("gezien", {})
    try:
        berichten, nieuw_wl, tel = verzamel(sessie, st)
    except LoginNodig:
        return {"status": "login_nodig"}
    nu = datetime.now(timezone.utc)
    uniek, ids = [], set()
    for b in sorted(berichten, key=lambda x: x["_ms"] or 0):
        if b["id"] in gezien or b["id"] in ids:
            continue
        ids.add(b["id"])
        uniek.append(b)
    dry = modus != "live"
    klassen, fouten_op_rij, verstuurd, ooit_fout = {}, 0, 0, False
    for i in range(0, len(uniek), BATCH):
        blok = uniek[i:i + BATCH]
        payload = [{k: v for k, v in b.items() if not k.startswith("_")} for b in blok]
        try:
            data = webhook.post("teams", {"dry_run": dry, "messages": payload}, timeout=WEBHOOK_TIMEOUT)
            if not isinstance(data, dict) or not data.get("ok"):
                raise RuntimeError("ok=false")
        except Exception as e:
            fouten_op_rij += 1
            ooit_fout = True
            log("teams_blok_mislukt", code=type(e).__name__, aantal=len(blok))
            if fouten_op_rij >= MAX_OPEENVOLGENDE_FOUTEN:
                webhook.alarm("teams", "webhook", "teams-classificatie-webhook faalt herhaaldelijk")
                break
            continue
        fouten_op_rij = 0
        verstuurd += len(blok)
        per_id = {r.get("id"): r for r in data.get("results") or [] if isinstance(r, dict)}
        for b in blok:
            k = (per_id.get(b["id"]) or {}).get("klasse")
            k = k if isinstance(k, str) else "geen_oordeel"
            klassen[k] = klassen.get(k, 0) + 1
            if not dry:
                gezien[b["id"]] = nu.isoformat()
        if not dry:
            staat.schrijf("teams", st)
    if not dry and not ooit_fout:
        st.setdefault("wl", {}).update(nieuw_wl)   # review MOET 2: één mislukt blok = waterlijnen blijven staan
    grens = (nu - timedelta(days=STATE_BEWAAR_DAGEN)).isoformat()
    st["gezien"] = {k: v for k, v in gezien.items() if v >= grens}
    staat.schrijf("teams", st)
    uit = {"status": "ok" if not ooit_fout else "webhook_fout", "modus": modus, "nieuw": len(uniek),
           "verstuurd": verstuurd, "chats": tel["chats"], "kanalen": tel["kanalen"], "fouten": tel["fouten"],
           "ids": ids_hash(b["id"] for b in uniek), **klassen}
    if verstuurd == 0 and not ooit_fout and not dry:
        try:
            webhook.levensteken("teams")
        except Exception:
            pass
    return uit
