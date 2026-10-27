"""Logboek als witte lijst (ontwerp § 4 Logs).

In het log komen alleen aantallen, tijden, foutcodes, HTTP-statussen en hosts. Nooit tekst, namen, onderwerpen, ids of
tokens. Velden buiten de lijst vallen weg; een waarde die niet op een code lijkt wordt "<weg>". Van stderr van
kindprocessen (Node-vanger, Chromium, x11vnc) komt alleen door wat op een witte-lijstpatroon past.
"""
import hashlib
import re
import sys
import time

VELDEN = frozenset({
    "dienst", "stap", "status", "code", "host", "http", "exit", "duur_s", "aantal", "nieuw", "verstuurd", "gezien",
    "zakelijk", "twijfel", "patientgebonden", "bekend", "geheim", "reclame", "overig", "fouten", "paginas", "bijlagen",
    "doorgelaten", "geweigerd", "modus", "ids", "reden", "soort", "kanalen", "chats", "groepen", "methode", "poging",
})
_WAARDE = re.compile(r"^[A-Za-z0-9_.:/+@-]{0,64}$")
_GEBEURTENIS = re.compile(r"^[a-z_]{1,40}$")
STDERR_PATRONEN = (
    re.compile(r"^exit=\d{1,3}$"),
    re.compile(r"^HTTP \d{3}$"),
    re.compile(r"^code=[A-Z_]{1,32}$"),
)


def schoon(v):
    if isinstance(v, bool):
        return "ja" if v else "nee"
    if isinstance(v, (int, float)):
        return str(round(v, 2)) if isinstance(v, float) else str(v)
    if isinstance(v, str) and _WAARDE.match(v):
        return v
    return "<weg>"


def regel(gebeurtenis, **velden):
    if not _GEBEURTENIS.match(gebeurtenis or ""):
        gebeurtenis = "onbekend"
    delen = [time.strftime("%Y-%m-%d %H:%M:%S"), gebeurtenis]
    weg = 0
    for k, v in velden.items():
        if k not in VELDEN:
            weg += 1
            continue
        delen.append(f"{k}={schoon(v)}")
    if weg:
        delen.append(f"weggelaten={weg}")
    return " ".join(delen)


def log(gebeurtenis, **velden):
    print(regel(gebeurtenis, **velden), file=sys.stdout, flush=True)


def filter_stderr(tekst):
    """Laat van kind-stderr alleen witte-lijstregels door; de rest telt alleen mee."""
    goed, weg = [], 0
    for r in (tekst or "").splitlines():
        r = r.strip()
        if not r:
            continue
        if any(p.match(r) for p in STDERR_PATRONEN):
            goed.append(r)
        else:
            weg += 1
    return goed, weg


def ids_hash(ids):
    """Kort, niet terug te rekenen kenmerk van een verzameling ids (voor 'zelfde ronde?'-vergelijking)."""
    h = hashlib.sha256("\n".join(sorted(str(i) for i in ids)).encode("utf-8")).hexdigest()
    return h[:12]
