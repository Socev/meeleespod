"""Naar n8n: dezelfde webhooks, velden en sleutels als de praktijk-pc (ontwerp § 5)."""
import json
import time
from datetime import datetime, timedelta, timezone

from . import instellingen as I
from . import staat, vangnet
from .logboek import log


def sleutel(naam):
    return (staat.lees("sleutels").get(naam) or "").strip()


def gekoppeld(dienst):
    return bool(sleutel(I.SLEUTEL_VOOR[dienst]))


def post(doel, payload, timeout=240, contact=True):
    """POST naar de vaste webhook `doel`. Geeft het JSON-antwoord (of {"ok": True, "raw": ...})."""
    geheim = sleutel(I.SLEUTEL_VOOR[doel])
    if not geheim:
        raise RuntimeError("geen webhooksleutel")
    p = dict(payload)
    p["secret"] = geheim
    p.setdefault("script_versie", I.SCRIPT_VERSIE)
    body = json.dumps(p, ensure_ascii=False).encode("utf-8")
    _, _, inhoud = vangnet.verzoek("POST", I.WEBHOOKS[doel], koppen={"Content-Type": "application/json"},
                                   data=body, timeout=timeout, dienst=doel.split("-")[0])
    if contact and not p.get("alarm"):
        dienst = doel.split("-")[0]
        staat.werk_bij("contact", lambda d: d.__setitem__(dienst, time.time()))
    try:
        return json.loads(inhoud.decode("utf-8"))
    except ValueError:
        return {"ok": True, "raw": True}


def alarm(dienst, soort, tekst):
    """Eén alarm `[dienst/soort] tekst`, hooguit 1x per interval per soort. Tekst = vaste zin van de pod zelf, nooit
    een Microsoft- of Parro-foutmelding (die kan een onderwerpregel of naam bevatten)."""
    if not gekoppeld(dienst):
        log("alarm_niet_verstuurd", dienst=dienst, soort=soort, reden="geen_sleutel")
        return False
    sleutelnaam = f"{dienst}/{soort}"
    nu = datetime.now(timezone.utc)
    uur = I.ALARM_HERHAAL_UUR.get(soort, I.ALARM_INTERVAL_UUR)
    laatst = staat.lees("alarmen").get(sleutelnaam)
    if laatst:
        try:
            if nu - datetime.fromisoformat(laatst) < timedelta(hours=uur):
                return False
        except ValueError:
            pass
    try:
        post(dienst, {"alarm": f"[{dienst}/{soort}] {tekst}"}, timeout=30, contact=False)
        staat.werk_bij("alarmen", lambda d: d.__setitem__(sleutelnaam, nu.isoformat()))
        log("alarm", dienst=dienst, soort=soort)
        return True
    except Exception as e:
        log("alarm_mislukt", dienst=dienst, soort=soort, code=type(e).__name__)
        return False


def wis_alarm(dienst, soort):
    staat.werk_bij("alarmen", lambda d: d.pop(f"{dienst}/{soort}", None))


def levensteken(dienst, extra=None):
    """Hooguit eens per LEVENSTEKEN_UUR zonder ander contact (zoals de pc)."""
    laatst = staat.lees("contact").get(dienst) or 0
    if time.time() - laatst < I.LEVENSTEKEN_UUR * 3600:
        return False
    post(dienst, {"levensteken": dict(extra or {"gezien": 0})}, timeout=30)
    log("levensteken", dienst=dienst)
    return True


vangnet._alarm_hook = alarm
