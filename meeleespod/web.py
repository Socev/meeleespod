"""Inlogpagina (ingang 'meeleespod', authLevel private: achter de Olares-login; poort 8080). Ontwerp § 6.

  GET  /                    stand per dienst + knop Inloggen (geen mail, geen berichten)
  GET  /stand.json          idem als JSON voor de pagina
  POST /inloggen?dienst=x   start het zichtbare inlogvenster; geeft een eenmalige code (alleen met de kop
                            X-Meelees-Knop en een Origin van deze host: een andere site kan dit niet laten doen)
  POST /inloggen/stop       venster dicht
  GET  /vnc/?code=...       noVNC-pagina; GET /vnc/ws?code=... = websocket naar x11vnc (code eenmalig, 2 min geldig)
  GET  /novnc/...           de vaste noVNC-bestanden uit het image
De Socev-API zit NIET op deze poort.
"""
import base64
import hashlib
import json
import mimetypes
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import instellingen as I
from . import planner, vangst
from .api import stand_openbaar
from .logboek import log

HIER = Path(__file__).resolve().parent / "pagina"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' wss: ws:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _zelfde_herkomst(handler):
    """Origin moet bestaan en dezelfde host hebben als Host of X-Forwarded-Host (de Olares-proxy kan Host herschrijven).
    Een andere site kan Origin niet vervalsen; de knop eist bovendien een eigen kop (dus een CORS-preflight)."""
    origin = handler.headers.get("Origin") or ""
    hosts = {(handler.headers.get(k) or "").split(",")[0].strip().split(":")[0].lower() for k in ("Host", "X-Forwarded-Host")}
    hosts.discard("")
    try:
        oh = (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return False
    return bool(origin) and bool(oh) and oh in hosts


class Pagina(BaseHTTPRequestHandler):
    server_version = "meeleespod"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _stuur(self, code, inhoud, soort="text/html; charset=utf-8", extra=None):
        b = inhoud if isinstance(inhoud, bytes) else inhoud.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", soort)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _json(self, code, data):
        self._stuur(code, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/gezond":
            return self._json(200, {"ok": True})
        if u.path == "/":
            return self._stuur(200, (HIER / "index.html").read_bytes())
        if u.path in ("/pagina.js", "/vnc.js", "/stijl.css"):
            soort = "text/css; charset=utf-8" if u.path.endswith(".css") else "text/javascript; charset=utf-8"
            return self._stuur(200, (HIER / u.path.lstrip("/")).read_bytes(), soort)
        if u.path == "/stand.json":
            s = stand_openbaar()
            return self._json(200, {"diensten": s["diensten"], "inlogvenster": s["inlogvenster"], "versie": s["versie"],
                                    "inlogvensters": s["inlogvensters"]})
        if u.path == "/vnc/":
            return self._stuur(200, (HIER / "vnc.html").read_bytes())
        if u.path == "/vnc/ws":
            return self._ws(parse_qs(u.query).get("code", [""])[0])
        if u.path.startswith("/novnc/"):
            return self._novnc(u.path[len("/novnc/"):])
        return self._json(404, {"fout": "onbekend"})

    def _novnc(self, rel):
        basis = I.NOVNC.resolve()
        try:
            p = (basis / rel).resolve()
        except (OSError, ValueError):
            return self._json(404, {"fout": "onbekend"})
        if basis not in p.parents or not p.is_file():
            return self._json(404, {"fout": "onbekend"})
        soort = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if p.suffix == ".js":
            soort = "text/javascript; charset=utf-8"
        return self._stuur(200, p.read_bytes(), soort)

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path not in ("/inloggen", "/inloggen/stop"):
            return self._json(404, {"fout": "onbekend"})
        if self.headers.get("X-Meelees-Knop") != "1" or not _zelfde_herkomst(self):
            log("knop_geweigerd", http=403)
            return self._json(403, {"fout": "alleen vanaf deze pagina"})
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(min(n, 1024))
        if u.path == "/inloggen/stop":
            vangst.sluit_inlogvenster()
            return self._json(200, {"ok": True})
        dienst = parse_qs(u.query).get("dienst", [""])[0]
        if dienst not in I.DIENSTEN:
            return self._json(400, {"fout": "onbekende dienst"})
        uit = vangst.start_inlogvenster(dienst, planner.na_vangst)
        return self._json(200 if "code" in uit else 409, uit)

    # ---- websocket ↔ x11vnc (eigen brug; geen websockify nodig) -------------------------------------------------------
    def _ws(self, code):
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            return self._json(400, {"fout": "websocket verwacht"})
        if not _zelfde_herkomst(self):
            return self._json(403, {"fout": "alleen vanaf deze pagina"})
        poort = vangst.neem_vnc_code(code)
        if not poort:
            log("vnc_geweigerd", http=403)
            return self._json(403, {"fout": "geen geldige code (eenmalig, 2 minuten geldig)"})
        try:
            vnc = socket.create_connection(("127.0.0.1", poort), timeout=10)
        except OSError:
            return self._json(502, {"fout": "inlogvenster niet bereikbaar"})
        sleutel = self.headers["Sec-WebSocket-Key"].strip()
        accept = base64.b64encode(hashlib.sha1((sleutel + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        if "binary" in (self.headers.get("Sec-WebSocket-Protocol") or ""):
            self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        self.wfile.flush()
        log("vnc_verbonden", status="ok")
        self.close_connection = True
        brug(self.connection, self.rfile, vnc)


def _lees_exact(f, n):
    b = f.read(n)
    if b is None or len(b) < n:
        raise EOFError
    return b


def _frame(opcode, data):
    kop = bytes([0x80 | opcode])
    n = len(data)
    if n < 126:
        kop += bytes([n])
    elif n < 65536:
        kop += bytes([126]) + struct.pack("!H", n)
    else:
        kop += bytes([127]) + struct.pack("!Q", n)
    return kop + data


def brug(klant, klant_in, vnc):
    """Websocket-frames van de browser ↔ ruwe TCP naar x11vnc. Stopt als een van beide kanten sluit."""
    schrijf_slot = threading.Lock()
    klaar = threading.Event()
    vnc.settimeout(None)
    klant.settimeout(None)

    def naar_klant(opcode, data):
        with schrijf_slot:
            klant.sendall(_frame(opcode, data))

    def van_vnc():
        try:
            while not klaar.is_set():
                d = vnc.recv(65536)
                if not d:
                    break
                naar_klant(2, d)
        except OSError:
            pass
        finally:
            klaar.set()
            try:
                naar_klant(8, b"")
            except OSError:
                pass

    t = threading.Thread(target=van_vnc, daemon=True)
    t.start()
    try:
        while not klaar.is_set():
            b1, b2 = _lees_exact(klant_in, 2)
            opcode, gemaskeerd, n = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", _lees_exact(klant_in, 2))[0]
            elif n == 127:
                n = struct.unpack("!Q", _lees_exact(klant_in, 8))[0]
            if not gemaskeerd or n > 16 * 1024 * 1024:
                break   # browser-frames zijn altijd gemaskeerd (RFC 6455)
            masker = _lees_exact(klant_in, 4)
            data = bytearray(_lees_exact(klant_in, n))
            for i in range(n):
                data[i] ^= masker[i % 4]
            if opcode == 8:
                break
            if opcode == 9:
                naar_klant(10, bytes(data))
                continue
            if opcode in (0, 1, 2):
                vnc.sendall(bytes(data))
    except (EOFError, OSError):
        pass
    finally:
        klaar.set()
        for s in (vnc,):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        log("vnc_dicht", status="ok")


def server(poort=8080):
    return ThreadingHTTPServer(("0.0.0.0", poort), Pagina)
