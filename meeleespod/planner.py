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
from . import staat, vangnet, vangst, webhook
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


def _fout(uitslag, standaard):
    """laatste_fout uit een vanger-/ronde-uitslag: code, en bij BROWSERSTART de subcode (witte lijst) en of de browser
    al gestart was (wv1950). Geen fouttekst."""
    f = {"tijd": nu_iso(), "code": str(uitslag.get("code") or standaard)[:32]}
    if uitslag.get("sub") in vangst.BROWSER_SUBCODES:
        f["sub"] = uitslag["sub"]
    if "gestart" in uitslag:
        f["gestart"] = bool(uitslag["gestart"])
    if uitslag.get("scherm") in vangst.SCHERMEN:
        f["scherm"] = uitslag["scherm"]
    # wv1958: bij Teams ook welke audiences er waren en de meting (al gefilterd in vangst.draai_vanger)
    if isinstance(uitslag.get("audiences"), dict):
        f["audiences"] = uitslag["audiences"]
    if isinstance(uitslag.get("diag"), dict):
        f["diag"] = uitslag["diag"]
    return f


def login_nodig(dienst):
    zet(dienst, login_nodig=True, laatste_fout={"tijd": nu_iso(), "code": "LOGIN_NODIG"})
    webhook.alarm(dienst, "login", LOGIN_TEKST[dienst])


def hersteld(dienst):
    if stand()[dienst].get("login_nodig"):
        zet(dienst, login_nodig=False)
        webhook.wis_alarm(dienst, "login")
        # B3-review ZOU 6: contacttijd weg, zodat de eerstvolgende ronde meteen een levensteken stuurt. n8n
        # (Pod-contact? → AI - Meeleespod-seintjes) zet daarop de login-rij dicht; anders kon David na een snelle
        # herinlog toch nog de herinnering "uitgelogd" krijgen.
        staat.werk_bij("contact", lambda d: d.pop(dienst, None))
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
        zet(dienst, laatste_fout=_fout(uitslag, "FOUT"))
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
                # wv1950: inlogscherm of MCAS-melding? (alleen de soort, nooit de url); wv1958: + vangercode, audiences, meting
                extra = {k: v for k, v in _fout(uitslag, "LOGIN_NODIG").items() if k in ("scherm", "audiences", "diag")}
                if uitslag.get("code") not in (None, "LOGIN_NODIG"):
                    extra["vanger"] = str(uitslag["code"])[:32]
                if extra:
                    zet(dienst, laatste_fout={**stand()[dienst].get("laatste_fout", {}), **extra})
                return {"status": "login_nodig"}
            else:
                n = int(s.get("vernieuw_fouten") or 0) + 1
                zet(dienst, vernieuw_fouten=n, laatste_fout=_fout(uitslag, "VERNIEUW_FOUT"))
                if n >= 3:
                    webhook.alarm(dienst, "vernieuwen", f"stil vernieuwen van {dienst} lukt al {n} keer op rij niet "
                                  "(netwerk of browser, geen uitlog)")
                return {"status": "vernieuw_fout"}
        uit = lezer.ronde(modus) if dienst != "parro" else lezer.ronde()
    except vangst.BrowserStartFout as e:
        uit = {"status": "fout", "code": "BROWSERSTART", "sub": e.sub, "gestart": False}
        log("ronde_fout", dienst=dienst, code="BROWSERSTART", soort=e.sub)
    except Exception as e:
        code = e.kort if isinstance(e, (vangnet.VerbindFout, vangnet.HttpFout)) else type(e).__name__
        uit = {"status": "fout", "code": code}
        log("ronde_fout", dienst=dienst, code=code)
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
        if dienst == "parro" and uit.get("herinlog") == "wachtwoord":
            # wv1951 (review KAN 9): zelf opnieuw ingelogd = een verse inlog (geen onterechte vooraankondiging na 80 dagen)
            velden.update(ingelogd_sinds=ronde["tijd"], vooraankondiging=None)
    else:
        velden["laatste_fout"] = {**_fout(uit, str(uit.get("status"))), "tijd": ronde["tijd"]}
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
