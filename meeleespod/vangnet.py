"""Vangnet: de enige uitgaande poort van de eigen code (ontwerp § 4).

- GET alleen via https naar een host uit instellingen.GET_HOSTS (poort 443, geen gebruikersnaam in de url).
- POST alleen naar een van de vaste webhook-adressen uit instellingen.WEBHOOKS (exact gelijk).
- Andere methodes nooit. Redirects worden nooit gevolgd.
- Een weigering is een alarm (via de webhook van de dienst), geen stille fout; de teller staat in /api/stand.

Eerlijk (ontwerp § 4): dit beperkt alleen de eigen Python-code. Chromium (nodig om in te loggen) en de Node-vanger
vallen erbuiten; een echte grens kan alleen op netwerkniveau (zie LEESMIJ, egress).
"""
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import instellingen as I
from .logboek import log

MAX_ANTWOORD = 48 * 1024 * 1024
_weigeringen = {"aantal": 0, "laatste_host": None, "laatste_tijd": None}
_slot = threading.Lock()
_alarm_hook = None   # gezet door webhook.py: functie(dienst, soort, tekst)


class Geweigerd(Exception):
    """Het vangnet weigerde een uitgaand verzoek."""
    def __init__(self, reden, host=""):
        super().__init__(reden)
        self.reden = reden
        self.host = host


class HttpFout(Exception):
    def __init__(self, status, host=""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.host = host


def weigeringen():
    with _slot:
        return dict(_weigeringen)


def _weiger(reden, host, dienst=None):
    with _slot:
        _weigeringen["aantal"] += 1
        _weigeringen["laatste_host"] = host[:64] if host else None
        _weigeringen["laatste_tijd"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    log("vangnet_weigert", reden=reden, host=host if host else "-", dienst=dienst or "-")
    if _alarm_hook and dienst:
        try:
            _alarm_hook(dienst, "vangnet", f"het vangnet weigerde een uitgaand verzoek ({reden})")
        except Exception:
            pass
    raise Geweigerd(reden, host)


def controleer_get(url, dienst=None):
    try:
        d = urlsplit(url)
    except ValueError:
        _weiger("url_onleesbaar", "", dienst)
    host = (d.hostname or "").lower()
    if d.scheme != "https":
        _weiger("geen_https", host, dienst)
    if d.username or d.password:
        _weiger("gebruiker_in_url", host, dienst)
    if d.port not in (None, 443):
        _weiger("vreemde_poort", host, dienst)
    if host not in I.GET_HOSTS:
        _weiger("host_niet_toegestaan", host, dienst)
    return host


def controleer_post(url, dienst=None):
    if url not in set(I.WEBHOOKS.values()):
        host = (urlsplit(url).hostname or "") if isinstance(url, str) else ""
        _weiger("post_buiten_webhooks", host.lower(), dienst)
    return urlsplit(url).hostname


class _GeenRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpFout(code, urlsplit(req.full_url).hostname or "")


_opener = urllib.request.build_opener(_GeenRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def verzoek(methode, url, koppen=None, data=None, timeout=60, dienst=None, max_bytes=MAX_ANTWOORD):
    """Doet het verzoek als het vangnet het toestaat. Geeft (status, koppen, bytes). 4xx/5xx → HttpFout."""
    methode = (methode or "").upper()
    if methode == "GET":
        host = controleer_get(url, dienst)
        if data is not None:
            _weiger("get_met_body", host, dienst)
    elif methode == "POST":
        host = controleer_post(url, dienst)
    else:
        _weiger("methode_niet_toegestaan", (urlsplit(url).hostname or "") if isinstance(url, str) else "", dienst)
    k = {"User-Agent": I.USER_AGENT}
    k.update(koppen or {})
    req = urllib.request.Request(url, data=data, headers=k, method=methode)
    try:
        with _opener.open(req, timeout=timeout) as r:
            inhoud = r.read(max_bytes + 1)
            if len(inhoud) > max_bytes:
                raise HttpFout(413, host)
            return r.status, dict(r.headers.items()), inhoud
    except urllib.error.HTTPError as e:
        try:
            e.read(1024)
        except Exception:
            pass
        raise HttpFout(e.code, host) from None


def get_json(url, koppen=None, timeout=60, dienst=None):
    _, _, inhoud = verzoek("GET", url, koppen=koppen, timeout=timeout, dienst=dienst)
    return json.loads(inhoud.decode("utf-8"))
