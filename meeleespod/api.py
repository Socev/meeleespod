"""De API voor Socev (ingang 'meeleesapi', authLevel internal; poort 8081). Ontwerp § 6.

Alleen met Socevs sleutel (in de chart alleen de sha256). Geeft NOOIT mail- of berichtinhoud:
  GET  /api/stand                                  stand per dienst (geldig tot, rondes, aantallen, foutcodes, versie)
  POST /api/ronde?dienst=outlook|teams|parro       nu een ronde (rem 1x per 10 min per dienst)
  POST /api/koppel   {"meelees": "...", "parro": "..."}   alleen de webhooksleutels; een adres zetten kan niet
  POST /api/modus?dienst=...&modus=proef|live|uit   proef = n8n classificeert maar bewaart niets (ontwerp § 8.1)
  POST /api/parro-proef                            zelf inloggen bij Parro proberen in een profielkopie zonder sessie
                                                   (wv1951; rem 1x per 10 min); uitkomst in stand → parro.wachtwoord
  POST /api/parro-gebruiker-vrij                   vastgezette Parro-gebruikersnaam loslaten (een ander account mag
                                                   daarna één keer op de inlogpagina gezet worden; review MOET 1)
  GET  /gezond                                     zonder sleutel
De inlogpagina en het inlogvenster zitten NIET op deze poort.
"""
import hashlib
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import instellingen as I
from . import parro_inlog, planner, staat, vangnet, vangst
from .logboek import log

MAX_BODY = 4096
_rem = {}
SLEUTEL_WAARDE = re.compile(r"^[A-Za-z0-9_\-.~+/=]{16,200}$")


def _hashes():
    return [h.strip().lower() for h in os.environ.get("MEELEES_SOCEV_HASHES", "").split(",") if len(h.strip()) == 64]


def stand_openbaar():
    """Wat /api/stand teruggeeft: alleen tijden, aantallen, codes en ja/nee."""
    s = planner.stand()
    sl = staat.lees("sleutels")
    uit = {"versie": I.VERSIE, "bijlagen": I.BIJLAGEN_AAN, "bezig": planner.bezig(), "inlogvenster": vangst.venster_stand(),
           "inlogvensters": (staat.lees("inlogvensters").get("starts") or [])[-5:],
           "vangnet": vangnet.weigeringen(), "diensten": {}}
    for d in I.DIENSTEN:
        x = s.get(d, {})
        lr = x.get("laatste_ronde") or {}
        uit["diensten"][d] = {
            "aan": I.DIENST_AAN[d], "modus": x.get("modus"), "sleutel_gezet": bool(sl.get(I.SLEUTEL_VOOR[d])),
            "ingelogd_sinds": x.get("ingelogd_sinds"), "geldig_tot": x.get("geldig_tot"),
            "login_nodig": bool(x.get("login_nodig")),
            "laatste_ronde": {k: v for k, v in lr.items() if isinstance(v, (int, float)) or k in ("tijd", "status", "modus", "code", "ids", "herinlog")},
            "laatste_met_berichten": x.get("laatste_met_berichten"), "laatste_fout": x.get("laatste_fout"),
        }
        if d == "parro":
            # wv1911: sessiemeting (namen, tijden, statussen, korte hashes) van de laatste 4 inlog-/rondemomenten
            uit["diensten"][d]["sessie"] = (staat.lees("parro_sessie").get("reeks") or [])[-4:]
            # wv1951: bewaard wachtwoord voor zelf opnieuw inloggen (alleen ja/nee, tijden, codes)
            uit["diensten"][d]["wachtwoord"] = parro_inlog.stand()
        if d == "teams" and "graph_mail_read" in x:
            uit["diensten"][d]["graph_mail_read"] = x["graph_mail_read"]
            uit["diensten"][d]["audiences"] = x.get("audiences")
    return uit


class Api(BaseHTTPRequestHandler):
    server_version = "meeleespod"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _json(self, code, data):
        b = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _sleutel_ok(self):
        k = self.headers.get("Authorization", "")
        if not k.startswith("Bearer "):
            return False
        h = hashlib.sha256(k[7:].strip().encode("utf-8")).hexdigest()
        return any(secrets.compare_digest(h, x) for x in _hashes())

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

    def _toegang(self):
        if not self._sleutel_ok():
            log("api_geweigerd", http=401, methode=self.command)
            self._json(401, {"fout": "sleutel ontbreekt of klopt niet"})
            return False
        return True

    def do_GET(self):
        pad = urlsplit(self.path).path
        if pad == "/gezond":
            return self._json(200, {"ok": True})
        if pad != "/api/stand":
            return self._json(404, {"fout": "onbekend"})
        if not self._toegang():
            return
        return self._json(200, stand_openbaar())

    def do_POST(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path not in ("/api/ronde", "/api/koppel", "/api/modus", "/api/parro-proef",
                          "/api/parro-gebruiker-vrij"):
            return self._json(404, {"fout": "onbekend"})
        if not self._toegang():
            return
        body = self._body()
        if body is None:
            return self._json(413, {"fout": "te groot"})
        if u.path == "/api/ronde":
            d = q.get("dienst")
            if d not in I.DIENSTEN:
                return self._json(400, {"fout": "dienst moet outlook, teams of parro zijn"})
            if time.time() - _rem.get(d, 0) < I.RONDE_REM_S:
                return self._json(429, {"fout": "hooguit één ronde per 10 minuten per dienst"})
            _rem[d] = time.time()
            planner.vraag_ronde(d)
            log("api_ronde", dienst=d)
            return self._json(202, {"ok": True, "dienst": d})
        if u.path == "/api/parro-gebruiker-vrij":
            parro_inlog.gebruiker_vrijgeven()
            return self._json(200, {"ok": True})
        if u.path == "/api/parro-proef":
            if time.time() - _rem.get("parro-proef", 0) < I.RONDE_REM_S:
                return self._json(429, {"fout": "hooguit één proef per 10 minuten"})
            if not parro_inlog.stand()["actief"]:
                return self._json(409, {"fout": "geen bruikbaar Parro-wachtwoord bewaard"})
            from . import parro
            if not parro.start_proef():
                return self._json(409, {"fout": "er loopt al een proef"})
            _rem["parro-proef"] = time.time()
            log("api_parro_proef", status="ok")
            return self._json(202, {"ok": True})
        if u.path == "/api/modus":
            if not planner.zet_modus(q.get("dienst"), q.get("modus")):
                return self._json(400, {"fout": "dienst outlook|teams|parro en modus proef|live|uit"})
            return self._json(200, {"ok": True})
        # /api/koppel: alleen sleutels, nooit een adres
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"fout": "geen JSON"})
        if not isinstance(data, dict) or not data or set(data) - set(I.SLEUTELNAMEN):
            log("api_koppel_geweigerd", http=400)
            return self._json(400, {"fout": "alleen de sleutels 'meelees' en/of 'parro'; adressen staan vast in het image"})
        for k, v in data.items():
            if not isinstance(v, str) or not SLEUTEL_WAARDE.match(v):
                return self._json(400, {"fout": f"sleutel {k} heeft geen geldige vorm"})
        staat.werk_bij("sleutels", lambda s: s.update(data))
        log("api_koppel", aantal=len(data))
        return self._json(200, {"ok": True, "gezet": sorted(data)})

    def do_PUT(self):
        self._json(405, {"fout": "methode niet toegestaan"})

    do_DELETE = do_PATCH = do_PUT


def server(poort=8081):
    return ThreadingHTTPServer(("0.0.0.0", poort), Api)
