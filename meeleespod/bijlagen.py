"""Bijlagenpad (ontwerp § 5.3) — een NIEUW patiëntpad, daarom streng:

1. Alleen voor een mail waarvoor n8n UITDRUKKELIJK klasse zakelijk of twijfel teruggaf. Ontbreekt het oordeel, is het
   onleesbaar of iets anders: behandeld als patiëntgebonden, geen bijlagen. (De pc viel bij een ontbrekend oordeel terug
   op "twijfel"; dat doet de pod níet.)
2. Alleen bestanden met een echte tekstlaag: pdf met tekst op elke pagina (gemiddeld ≥ 200 tekens per pagina, geen
   pagina vrijwel leeg) en zonder scanachtig beeld; docx en xlsx (tekst uit de XML), zonder foto's erin.
   Scans en foto's (jpg, png, heic, tiff, pdf zonder tekstlaag) gaan NOOIT door, ook niet na OCR: een beeld kan
   patiëntinformatie tonen die de tekst niet laat zien.
3. Geen zip, eml, msg, uitvoerbaar of onbekend formaat. Het soort bestand komt uit de inhoud (magische bytes), niet uit
   de naam of het opgegeven type.
Bytes blijven in het geheugen; pdftotext krijgt een bestand op de tmpfs /werk dat meteen weer weg is.
"""
import hashlib
import html
import io
import os
import re
import secrets
import subprocess
import zipfile

from . import instellingen as I

MAX_PER_MAIL = 10
MAX_BYTES = 20 * 1024 * 1024
MIN_GEMIDDELD_PER_PAGINA = 200
MIN_PER_PAGINA = 40
SCAN_PIXELS = 1000                 # een beeld van ≥ 1000 x 1000 px in een pdf = scan of foto: weigeren
MAX_MEDIA_BYTES = 200 * 1024       # een plaatje > 200 kB in docx/xlsx = foto: weigeren (logo's zijn kleiner)
MAX_TEKST = 200_000
TOEGESTAAN = ("zakelijk", "twijfel")


def mag_bijlagen(oordeel):
    """Alleen een expliciet oordeel {klasse: zakelijk|twijfel} telt."""
    return isinstance(oordeel, dict) and isinstance(oordeel.get("klasse"), str) and oordeel["klasse"] in TOEGESTAAN


def soort_uit_inhoud(b):
    if b[:5] == b"%PDF-":
        return "pdf"
    if b[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(b)) as z:
                namen = set(z.namelist())
        except zipfile.BadZipFile:
            return "onbekend"
        if "word/document.xml" in namen:
            return "docx"
        if "xl/workbook.xml" in namen:
            return "xlsx"
        return "zip"
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if b[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"):
        return "heic"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"\xd0\xcf\x11\xe0":
        return "ole"   # oude Office of .msg: niet
    if b[:2] == b"MZ" or b[:4] == b"\x7fELF":
        return "uitvoerbaar"
    return "onbekend"


def _weiger(reden, soort="onbekend"):
    return {"door": False, "reden": reden, "soort": soort, "tekst": "", "sha256": ""}


def _pdf(b):
    I.WERK.mkdir(parents=True, exist_ok=True, mode=0o700)
    pad = I.WERK / f"b-{secrets.token_hex(8)}.pdf"
    fd = os.open(pad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", "-layout", str(pad), "-"], capture_output=True,
                           timeout=60)
        if r.returncode != 0:
            return None, "pdf_onleesbaar"
        tekst = r.stdout.decode("utf-8", "replace")
        beelden = subprocess.run(["pdfimages", "-list", str(pad)], capture_output=True, timeout=60)
        if beelden.returncode == 0:
            for regel in beelden.stdout.decode("utf-8", "replace").splitlines()[2:]:
                kol = regel.split()
                if len(kol) > 4 and kol[3].isdigit() and kol[4].isdigit():
                    if int(kol[3]) >= SCAN_PIXELS and int(kol[4]) >= SCAN_PIXELS:
                        return None, "pdf_scanbeeld"
        else:
            return None, "pdf_beelden_onbekend"
        return tekst, None
    finally:
        try:
            os.unlink(pad)
        except OSError:
            pass


def _office(b, soort):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        for info in z.infolist():
            if info.file_size > 50 * 1024 * 1024:
                return None, "office_te_groot"
            if re.match(r"^(word|xl)/media/", info.filename) and info.file_size > MAX_MEDIA_BYTES:
                return None, "office_foto"
            if re.match(r"^(word|xl)/embeddings/", info.filename):
                return None, "office_ingesloten"
        delen = []
        if soort == "docx":
            namen = ["word/document.xml"] + sorted(n for n in z.namelist() if re.match(r"^word/(header|footer)\d*\.xml$", n))
            for n in namen:
                xml = z.read(n).decode("utf-8", "replace")
                xml = re.sub(r"</w:p>", "\n", xml)
                delen.extend(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
        else:
            for n in sorted(x for x in z.namelist() if x == "xl/sharedStrings.xml" or re.match(r"^xl/worksheets/sheet\d+\.xml$", x)):
                xml = z.read(n).decode("utf-8", "replace")
                delen.extend(re.findall(r"<t[^>]*>([^<]*)</t>", xml))
                delen.extend(re.findall(r"<v>([^<]*)</v>", xml))
        return html.unescape(" ".join(delen)), None


def beoordeel(naam, inhoud):
    """Geeft {door, reden, soort, tekst, sha256}. door=True alleen bij een echte tekstlaag zonder scan/foto."""
    if not isinstance(inhoud, (bytes, bytearray)) or not inhoud:
        return _weiger("leeg")
    if len(inhoud) > MAX_BYTES:
        return _weiger("te_groot")
    soort = soort_uit_inhoud(bytes(inhoud))
    if soort in ("jpg", "png", "tiff", "heic", "gif"):
        return _weiger("beeld", soort)
    if soort not in ("pdf", "docx", "xlsx"):
        return _weiger("formaat_niet_toegestaan", soort)
    try:
        if soort == "pdf":
            tekst, fout = _pdf(bytes(inhoud))
            if fout:
                return _weiger(fout, soort)
            paginas = tekst.split("\f")
            if paginas and not paginas[-1].strip():
                paginas = paginas[:-1]
            if not paginas:
                return _weiger("pdf_geen_tekstlaag", soort)
            lengtes = [len(re.sub(r"\s+", "", p)) for p in paginas]
            if min(lengtes) < MIN_PER_PAGINA or sum(lengtes) / len(lengtes) < MIN_GEMIDDELD_PER_PAGINA:
                return _weiger("pdf_geen_tekstlaag", soort)
        else:
            tekst, fout = _office(bytes(inhoud), soort)
            if fout:
                return _weiger(fout, soort)
            if len(re.sub(r"\s+", "", tekst or "")) < MIN_GEMIDDELD_PER_PAGINA // 2:
                return _weiger("office_te_weinig_tekst", soort)
    except (subprocess.SubprocessError, zipfile.BadZipFile, OSError, KeyError):
        return _weiger("onleesbaar", soort)
    return {"door": True, "reden": "tekstlaag", "soort": soort, "tekst": tekst[:MAX_TEKST],
            "sha256": hashlib.sha256(inhoud).hexdigest()}
