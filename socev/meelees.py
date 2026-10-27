#!/usr/bin/env python3
"""Client voor de meeleespod-API, vanuit de claudebot-pod (machinekamer). Geeft nooit mail- of berichtinhoud: die
heeft de API niet.

  meelees.py stand                         stand per dienst (JSON)
  meelees.py ronde <outlook|teams|parro>   nu een ronde (rem 1x per 10 min)
  meelees.py modus <dienst> <proef|live|uit>
  meelees.py koppel <json-bestand>         webhooksleutels zetten uit een bestand {"meelees": ..., "parro": ...}
                                           (nooit op de opdrachtregel); schrijft een box-regel (ontwerp § 6)

Sleutel: /opt/data/.meeleespod/sleutel (0600). Adres van de API-ingang (meeleesapi, authLevel internal):
/opt/data/.meeleespod/adres (één regel, https://...; wordt na de installatie gezet). Eigen User-Agent (Cloudflare 1010).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

MAP = os.environ.get("MEELEES_MAP", "/opt/data/.meeleespod")
BOX = "/opt/data/AI_SecondBrain/00_Systeem/Meldingen/machinekamer.md"


def _lees(naam):
    with open(os.path.join(MAP, naam), encoding="utf-8") as f:
        return f.read().strip()


def vraag(methode, pad, body=None):
    adres = _lees("adres").rstrip("/")
    if not adres.startswith("https://"):
        raise SystemExit("adres moet met https:// beginnen")
    data = json.dumps(body).encode("utf-8") if body is not None else (b"" if methode == "POST" else None)
    req = urllib.request.Request(adres + pad, data=data, method=methode, headers={
        "Authorization": "Bearer " + _lees("sleutel"), "Content-Type": "application/json",
        "User-Agent": "Socev-machinekamer/1 (meelees.py)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {"fout": f"HTTP {e.code}"}


def box_regel(tekst):
    sys.path.insert(0, "/opt/data/mk-scripts/veilig")
    import vault_veilig
    vault_veilig.voeg_toe(BOX, f"\n- {time.strftime('%d-%m-%Y %H:%M')} — meeleespod: {tekst} (meelees.py koppel)\n")


def main(a):
    if not a or a[0] not in ("stand", "ronde", "modus", "koppel"):
        print(__doc__)
        return 2
    if a[0] == "stand":
        st, d = vraag("GET", "/api/stand")
    elif a[0] == "ronde" and len(a) == 2:
        st, d = vraag("POST", f"/api/ronde?dienst={a[1]}")
    elif a[0] == "modus" and len(a) == 3:
        st, d = vraag("POST", f"/api/modus?dienst={a[1]}&modus={a[2]}")
    elif a[0] == "koppel" and len(a) == 2:
        with open(a[1], encoding="utf-8") as f:
            sleutels = json.load(f)
        st, d = vraag("POST", "/api/koppel", sleutels)
        if st == 200:
            box_regel("webhooksleutel(s) gezet: " + ", ".join(d.get("gezet") or []) + " (alleen namen)")
    else:
        print(__doc__)
        return 2
    print(json.dumps({"http": st, **d}, ensure_ascii=False, indent=1))
    return 0 if 200 <= st < 300 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
