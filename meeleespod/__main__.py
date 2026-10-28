"""python -m meeleespod [pod|zelftoets]

pod        planner + inlogpagina (8080) + API (8081). Het virtuele scherm start pas als een browser het nodig heeft.
zelftoets  rooktest in het image zonder netwerk en zonder inlog: vangnet, logfilter, bijlagen-oordeel, mappen.
beleidtoets  start de Chromium uit het image zichtbaar op het virtuele scherm en toetst dat het beleid geldt.
"""
import os
import signal
import sys
import threading

from . import instellingen as I


def pod():
    from . import api, planner, web
    from .logboek import log
    os.umask(0o077)
    for p in (I.STAAT, I.GEHEIM, I.WERK):
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    s1, s2 = web.server(8080), api.server(8081)
    for s in (s1, s2):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    threading.Thread(target=planner.lus, args=(stop,), daemon=True).start()
    log("gestart", status="ok", code=I.VERSIE)

    def einde(*_):
        stop.set()
        from . import vangst
        vangst.sluit_inlogvenster()
        for s in (s1, s2):
            s.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, einde)
    signal.signal(signal.SIGINT, einde)
    stop.wait()


def zelftoets():
    from . import bijlagen, vangnet
    from .logboek import filter_stderr, regel
    fout = []

    def eis(naam, ok):
        print(("  OK    " if ok else "  FOUT  ") + naam)
        if not ok:
            fout.append(naam)

    def geweigerd(f):
        try:
            f()
        except vangnet.Geweigerd:
            return True
        except Exception:
            return False
        return False
    eis("vangnet weigert GET naar een vreemde host", geweigerd(lambda: vangnet.verzoek("GET", "https://example.com/")))
    eis("vangnet weigert http zonder s", geweigerd(lambda: vangnet.verzoek("GET", "http://outlook.office.com/api/v2.0/me")))
    eis("vangnet weigert andere poort", geweigerd(lambda: vangnet.verzoek("GET", "https://outlook.office.com:8443/x")))
    eis("vangnet weigert POST naar Outlook (verzenden)", geweigerd(lambda: vangnet.verzoek(
        "POST", "https://outlook.office.com/api/v2.0/me/sendmail", data=b"{}")))
    eis("vangnet weigert POST naar Teams-chat", geweigerd(lambda: vangnet.verzoek(
        "POST", "https://teams.microsoft.com/api/chatsvc/emea/v1/users/ME/conversations/x/messages", data=b"{}")))
    eis("vangnet weigert PATCH en DELETE", geweigerd(lambda: vangnet.verzoek("PATCH", "https://graph.microsoft.com/v1.0/me"))
        and geweigerd(lambda: vangnet.verzoek("DELETE", I.WEBHOOKS["outlook"])))
    eis("vangnet weigert POST naar een webhook-lookalike", geweigerd(lambda: vangnet.verzoek(
        "POST", I.WEBHOOKS["outlook"] + "?x=1", data=b"{}")))
    eis("vangnet telt weigeringen", vangnet.weigeringen()["aantal"] >= 8)
    r = regel("ronde", dienst="outlook", aantal=3, onderwerp="Uitslag lab mw. Jansen", code="Bericht van Jansen met spaties")
    eis("logfilter laat geen tekst of vreemde velden door", "Jansen" not in r and "onderwerp" not in r and "code=<weg>" in r)
    goed, weg = filter_stderr("exit=4\n[teams-cli login] detected region: emea\nAADSTS50076 user jan@praktijk.nl\nHTTP 401\n")
    eis("stderr-filter: alleen witte lijst", goed == ["exit=4", "HTTP 401"] and weg == 2)
    eis("geen oordeel = geen bijlagen", not bijlagen.mag_bijlagen(None) and not bijlagen.mag_bijlagen({})
        and not bijlagen.mag_bijlagen({"klasse": "patientgebonden"}) and not bijlagen.mag_bijlagen({"klasse": None})
        and bijlagen.mag_bijlagen({"klasse": "zakelijk"}))
    eis("terugval-oordeel van n8n = geen bijlagen", not bijlagen.mag_bijlagen({"klasse": "twijfel", "terugval": True}))
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 2000
    eis("jpg gaat nooit door", bijlagen.beoordeel("brief.pdf", jpg)["door"] is False)
    from .toetsdata import pdf_met_tekst, pdf_zonder_tekst
    eis("pdf zonder tekstlaag (scan) gaat niet door", bijlagen.beoordeel("scan.pdf", pdf_zonder_tekst())["door"] is False)
    eis("pdf met tekstlaag gaat door", bijlagen.beoordeel("offerte.pdf", pdf_met_tekst())["door"] is True)
    from .toetsdata import pdf_met_annotatie
    eis("pdf met notitie of formulier gaat niet door (poppler in dit image)",
        bijlagen.beoordeel("n.pdf", pdf_met_annotatie(b"Text"))["reden"] == "pdf_opmerkingen"
        and bijlagen.beoordeel("f.pdf", pdf_met_annotatie(b"Widget", formulier=True))["reden"] == "pdf_formulier")
    eis("pdf met alleen een link gaat door", bijlagen.beoordeel("l.pdf", pdf_met_annotatie(b"Link"))["door"] is True)
    print("zelftoets:", "GESLAAGD" if not fout else f"{len(fout)} FOUT")
    return 0 if not fout else 1


def beleidtoets():
    """De productie-Chromium (zichtbaar op Xvfb, zoals bij inloggen) moet chrome:// weigeren: dan is het beleid geladen."""
    import tempfile
    from playwright.sync_api import sync_playwright
    from . import vangst
    vangst.zorg_voor_scherm()
    omg = vangst.kind_omgeving("parro")
    os.environ["DISPLAY"], os.environ["XAUTHORITY"] = omg["DISPLAY"], omg["XAUTHORITY"]
    uitkomsten = {}
    with sync_playwright() as pw, tempfile.TemporaryDirectory() as prof:
        ctx = pw.chromium.launch_persistent_context(prof, headless=False, accept_downloads=False, timeout=60000)
        p = ctx.new_page()
        # chrome://version = bewijs dat het beleid geladen is; devtools:// = het ontwikkelaarsgereedschap kan niet laden
        for url in ("chrome://version", "devtools://devtools/bundled/inspector.html", "file:///etc/passwd"):
            try:
                p.goto(url, timeout=15000)
                uitkomsten[url] = "NIET geblokkeerd"
            except Exception as e:
                uitkomsten[url] = "geblokkeerd" if "ERR_BLOCKED_BY_ADMINISTRATOR" in str(e) else "andere fout: " + type(e).__name__
        ctx.close()
    for url, u in uitkomsten.items():
        print(("  OK    " if u == "geblokkeerd" else "  FOUT  ") + f"beleid {url}: {u}")
    return 0 if all(u == "geblokkeerd" for u in uitkomsten.values()) else 1


if __name__ == "__main__":
    wat = sys.argv[1] if len(sys.argv) > 1 else "pod"
    if wat == "zelftoets":
        sys.exit(zelftoets())
    if wat == "beleidtoets":
        sys.exit(beleidtoets())
    pod()
