"""Planner en stand per dienst (ontwerp § 4 planner, § 7 seintje en hartslag).

- Een dienst draait pas na de eerste geslaagde handmatige inlog; daarvoor geen rondes en geen alarm.
- Modus per dienst: proef (dry_run: n8n classificeert, bewaart niets), live, of uit. Outlook en Teams beginnen in proef
  (ontwerp § 8.1), Parro begint live met zaaien (§ 8.2). Omzetten via /api/modus.
  Proef draait NOOIT vanzelf (review MOET 5: elk uur het hele venster opnieuw naar Qwen); alleen via /api/ronde.
- Login nodig: één alarm [dienst/login] bij de overgang, daarna hooguit 1x per 24 u; hersteld = alarmstempel weg.
- Vooraankondiging: 80 dagen na de laatste handmatige inlog één bericht.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from . import instellingen as I
from . import staat, vangst, webhook
from .logboek import log

STANDAARD_MODUS = {"outlook": "proef", "teams": "proef", "parro": "live"}
LOGIN_TEKST = {
    "outlook": "Outlook van de praktijk is uitgelogd: tik op de inlogpagina van de meeleespod op Inloggen bij Outlook en log één keer in",
    "teams": "Teams van de praktijk is uitgelogd: tik op de inlogpagina van de meeleespod op Inloggen bij Teams en log één keer in",
    "parro": "Parro is uitgelogd: tik op de inlogpagina van de meeleespod op Inloggen bij Parro en log één keer in",
}
_wachtrij = set()
_wachtrij_slot = threading.Lock()
_bezig = {"dienst": None}


def nu_iso():
    return datetime.now(timezone.utc).isoformat()


def stand():
    s = staat.lees("stand")
    for d in I.DIENSTEN:
        s.setdefault(d, {})
        s[d].setdefault("modus", STANDAARD_MODUS[d])
    return s


def zet(dienst, **velden):
    def f(s):
        s.setdefault(dienst, {}).update(velden)
    staat.werk_bij("stand", f)


def zet_modus(dienst, modus):
    if dienst not in I.DIENSTEN or modus not in ("proef", "live", "uit"):
        return False
    zet(dienst, modus=modus)
    log("modus", dienst=dienst, modus=modus)
    return True


def vraag_ronde(dienst):
    with _wachtrij_slot:
        _wachtrij.add(dienst)


def login_nodig(dienst):
    zet(dienst, login_nodig=True, laatste_fout={"tijd": nu_iso(), "code": "LOGIN_NODIG"})
    webhook.alarm(dienst, "login", LOGIN_TEKST[dienst])


def hersteld(dienst):
    if stand()[dienst].get("login_nodig"):
        zet(dienst, login_nodig=False)
        webhook.wis_alarm(dienst, "login")
        log("login_hersteld", dienst=dienst)


def na_vangst(dienst, code, uitslag):
    """Na het zichtbare inlogvenster."""
    if code == 0 and uitslag.get("ok"):
        velden = {"ingelogd_sinds": nu_iso(), "geldig_tot": uitslag.get("geldig_tot"), "vooraankondiging": None}
        if dienst == "teams":
            velden["graph_mail_read"] = bool(uitslag.get("graph_mail_read"))   # meting ontwerp § 4 (Graph als 2e route)
            velden["audiences"] = uitslag.get("audiences") or {}
        zet(dienst, **velden)
        hersteld(dienst)
        webhook.wis_alarm(dienst, "vooraankondiging")
        vraag_ronde(dienst)
        log("ingelogd", dienst=dienst)
    else:
        zet(dienst, laatste_fout={"tijd": nu_iso(), "code": str(uitslag.get("code") or "FOUT")[:32]})
        log("inloggen_niet_gelukt", dienst=dienst, code=str(uitslag.get("code") or "FOUT"))


def _lezer(dienst):
    if dienst == "outlook":
        from . import outlook
        return outlook
    if dienst == "teams":
        from . import teams
        return teams
    from . import parro
    return parro


def draai(dienst, handmatig=False):
    """Eén ronde voor `dienst`, inclusief stil vernieuwen. Schrijft de uitkomst in de stand."""
    s = stand()[dienst]
    modus = s.get("modus", STANDAARD_MODUS[dienst])
    if modus == "uit" or not s.get("ingelogd_sinds") or not I.DIENST_AAN[dienst]:
        return None
    if modus == "proef" and not handmatig:
        return None
    if not webhook.gekoppeld(dienst):
        zet(dienst, laatste_fout={"tijd": nu_iso(), "code": "GEEN_SLEUTEL"})
        return None
    _bezig["dienst"] = dienst
    t0 = time.time()
    try:
        lezer = _lezer(dienst)
        if dienst in ("outlook", "teams") and lezer.vernieuw_nodig():
            if not vangst.browser_slot.acquire(timeout=600):
                zet(dienst, laatste_fout={"tijd": nu_iso(), "code": "BROWSER_BEZET"})
                return None
            try:
                code, uitslag = vangst.draai_vanger(dienst, "stil", 300)
                vangst.wis_berichtcache(dienst)
            finally:
                vangst.browser_slot.release()
            if code == 0 and uitslag.get("ok"):
                velden = {"geldig_tot": uitslag.get("geldig_tot"), "vernieuw_fouten": 0}
                if s.get("vooraankondiging"):
                    # ontwerp § 7: lukt stil vernieuwen na de vooraankondiging gewoon, dan schuift de datum mee
                    velden.update(ingelogd_sinds=nu_iso(), vooraankondiging=None)
                zet(dienst, **velden)
            elif code == 4:
                login_nodig(dienst)
                return {"status": "login_nodig"}
            else:
                n = int(s.get("vernieuw_fouten") or 0) + 1
                zet(dienst, vernieuw_fouten=n,
                    laatste_fout={"tijd": nu_iso(), "code": str(uitslag.get("code") or "VERNIEUW_FOUT")[:32]})
                if n >= 3:
                    webhook.alarm(dienst, "vernieuwen", f"stil vernieuwen van {dienst} lukt al {n} keer op rij niet "
                                  "(netwerk of browser, geen uitlog)")
                return {"status": "vernieuw_fout"}
        uit = lezer.ronde(modus) if dienst != "parro" else lezer.ronde()
    except Exception as e:
        uit = {"status": "fout", "code": type(e).__name__}
        log("ronde_fout", dienst=dienst, code=type(e).__name__)
    finally:
        _bezig["dienst"] = None
    uit["duur_s"] = round(time.time() - t0, 1)
    ronde = {"tijd": nu_iso(), **{k: v for k, v in uit.items() if isinstance(v, (int, float, str)) or v is None}}
    velden = {"laatste_ronde": ronde}
    if uit.get("status") == "niet_ingelogd":
        # review MOET 3: sessiebestand weg (app-cache leeg na upgrade/herplaatsing) terwijl er ooit is ingelogd
        login_nodig(dienst)
    elif uit.get("status") == "login_nodig":
        login_nodig(dienst)
    elif uit.get("status") == "ok":
        hersteld(dienst)
        if uit.get("verstuurd"):
            velden["laatste_met_berichten"] = ronde["tijd"]
        if dienst == "parro" and uit.get("geldig_tot"):
            velden["geldig_tot"] = uit["geldig_tot"]
    else:
        velden["laatste_fout"] = {"tijd": ronde["tijd"], "code": str(uit.get("code") or uit.get("status"))[:32]}
    zet(dienst, **velden)
    log("ronde", **{k: v for k, v in ronde.items() if k != "tijd"}, dienst=dienst)
    return uit


def _vooraankondiging():
    for d in I.DIENSTEN:
        s = stand()[d]
        sinds = s.get("ingelogd_sinds")
        if not sinds or s.get("vooraankondiging") or not I.DIENST_AAN[d] or s.get("modus") == "uit":
            continue
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(sinds) >= timedelta(days=I.VOORAANKONDIGING_DAGEN):
                if webhook.alarm(d, "vooraankondiging", f"de inlog bij {d} is {I.VOORAANKONDIGING_DAGEN} dagen oud; binnenkort "
                                 "opnieuw inloggen via de inlogpagina van de meeleespod voorkomt een onderbreking"):
                    zet(d, vooraankondiging=nu_iso())
        except ValueError:
            continue


def _gepland(dienst, nu, laatst):
    if nu.hour not in I.UREN[dienst] or nu.minute < I.MINUUT[dienst]:
        return False
    slot = nu.strftime("%Y-%m-%dT%H")
    return laatst.get(dienst) != slot


def lus(stop):
    laatst = staat.lees("planner")
    while not stop.is_set():
        try:
            nu = datetime.now()
            for d in I.DIENSTEN:
                with _wachtrij_slot:
                    handmatig = d in _wachtrij
                    _wachtrij.discard(d)
                if handmatig or _gepland(d, nu, laatst):
                    if not handmatig:
                        laatst[d] = nu.strftime("%Y-%m-%dT%H")
                        staat.schrijf("planner", laatst)
                    draai(d, handmatig=handmatig)
            if nu.minute == 0:
                _vooraankondiging()
        except Exception as e:
            log("planner_fout", code=type(e).__name__)
        stop.wait(20)


def bezig():
    return _bezig["dienst"]
