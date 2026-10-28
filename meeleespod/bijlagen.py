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
import zlib
import zipfile

from . import instellingen as I

MAX_PER_MAIL = 10
# Grenzen gelijk aan n8n (review ZOU 3): n8n weigert boven 11 MB (de body is base64, n8n neemt standaard ≤ 16 MB aan)
# en een tekst boven 30.000 tekens (JavaScript-lengte, na trim). Langer = niet versturen; alleen de naam blijft bij de mail.
MAX_BYTES = 11 * 1024 * 1024
MIN_GEMIDDELD_PER_PAGINA = 200
MIN_PER_PAGINA = 40
# Beelden (review MOET 7): elk beeld waarvan de kortste zijde ≥ 300 px is of dat ≥ 250.000 px beslaat, is een foto of
# scan en laat de hele bijlage vallen. Een briefhoofdlogo (bv. 600 x 200) mag. Een docx/xlsx met een plaatje > 50 kB,
# of een pdf met ingesloten bestanden, valt ook.
BEELD_MIN_ZIJDE = 300
BEELD_MAX_OPPERVLAK = 250_000
MAX_MEDIA_BYTES = 50 * 1024
MAX_TEKST = 30_000
MAX_OFFICE_TOTAAL = 100 * 1024 * 1024
MAX_OFFICE_DELEN = 2000
MAX_PDF_STROMEN = 64 * 1024 * 1024
TOEGESTAAN = ("zakelijk", "twijfel")


def mag_bijlagen(oordeel):
    """Alleen een expliciet oordeel {klasse: zakelijk|twijfel} dat Qwen zelf gaf. n8n zet `terugval: true` als het oordeel
    een terugval op twijfel is (Qwen gaf niets bruikbaars, review MOET 1): dat telt niet."""
    return (isinstance(oordeel, dict) and isinstance(oordeel.get("klasse"), str) and oordeel["klasse"] in TOEGESTAAN
            and oordeel.get("terugval") in (None, False))


def js_lengte(tekst):
    """Lengte zoals n8n (JavaScript) hem telt: na weghalen van NUL en trim, in UTF-16-eenheden."""
    t = tekst.replace("\x00", "").strip()
    return len(t.encode("utf-16-le", "surrogatepass")) // 2


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


# ---- pdf: formulieren en opmerkingen (review MOET 3) -----------------------------------------------------------------
# Gemeten 28-10-2026 (poppler 22.12, verzonnen pdf): pdftotext neemt de waarde van een formulierveld en een vrij
# tekstvak (FreeText) mee, maar NIET de tekst van een notitie (/Text, sticky note) of andere opmerkingen; een verborgen
# veld tekent hij ook niet. Daarom: een pdf met een formulier (pdfinfo "Form:" ≠ none) of met een annotatie anders dan
# een link gaat niet door. De annotaties staan vaak in gecomprimeerde objectstromen: die pakken we uit (alleen Flate);
# een objectstroom die niet uit te pakken is, of een versleutelde pdf, gaat ook niet door (dan zien we niet wat erin staat).
PDF_ANNOT_SOORTEN = frozenset({
    "Text", "FreeText", "Line", "Square", "Circle", "Polygon", "PolyLine", "Highlight", "Underline", "Squiggly",
    "StrikeOut", "Stamp", "Caret", "Ink", "Popup", "FileAttachment", "Sound", "Movie", "Widget", "Screen",
    "PrinterMark", "TrapNet", "Watermark", "3D", "Redact", "RichMedia", "Projection"})
_PDF_STROOM = re.compile(rb"\bobj\b(.{0,4096}?)\bstream\r?\n", re.S)
_PDF_NAAM_HEX = re.compile(rb"#([0-9A-Fa-f]{2})")


def _pdf_naam_normaal(b):
    """PDF-namen mogen #xx-codes bevatten (/Sub#74ype); voor het zoeken alles terugvertalen (vals alarm = weigering)."""
    return _PDF_NAAM_HEX.sub(lambda m: bytes([int(m.group(1), 16)]), b)


def pdf_structuur(b):
    """Geeft None als er geen formulier/opmerking/versleuteling te vinden is, anders een redencode."""
    delen = [b]
    totaal = 0
    for m in _PDF_STROOM.finditer(b):
        kop = _pdf_naam_normaal(m.group(1))
        if b"/ObjStm" not in kop:
            continue   # alleen objectstromen dragen objecten (annotaties, formulier); paginastromen niet
        eind = b.find(b"endstream", m.end())
        if eind < 0 or b"/DecodeParms" in kop or b"/FlateDecode" not in kop or re.search(rb"/(ASCII|LZW|RunLength|Crypt)", kop):
            return "pdf_structuur_onleesbaar"
        try:
            d = zlib.decompressobj()
            uit = d.decompress(b[m.end():eind], MAX_PDF_STROMEN - totaal)
        except zlib.error:
            return "pdf_structuur_onleesbaar"
        if d.unconsumed_tail:
            return "pdf_structuur_te_groot"
        totaal += len(uit)
        delen.append(uit)
    alles = _pdf_naam_normaal(b"\n".join(delen))
    if re.search(rb"/Encrypt\b", alles):
        return "pdf_versleuteld"
    if re.search(rb"/AcroForm\b", alles) or re.search(rb"/XFA\b", alles):
        return "pdf_formulier"
    for s in re.findall(rb"/Subtype\s*/([A-Za-z0-9]+)", alles):
        if s.decode("latin-1") in PDF_ANNOT_SOORTEN:
            return "pdf_opmerkingen"
    return None


def _pdf(b):
    fout = pdf_structuur(b)
    if fout:
        return None, fout
    I.WERK.mkdir(parents=True, exist_ok=True, mode=0o700)
    pad = I.WERK / f"b-{secrets.token_hex(8)}.pdf"
    fd = os.open(pad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        info = subprocess.run(["pdfinfo", str(pad)], capture_output=True, timeout=60)
        if info.returncode != 0:
            return None, "pdf_onleesbaar"
        vorm = re.search(r"^Form:\s*(\S+)", info.stdout.decode("utf-8", "replace"), re.M)
        if not vorm or vorm.group(1).lower() != "none":
            return None, "pdf_formulier"
        r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", "-layout", str(pad), "-"], capture_output=True,
                           timeout=60)
        if r.returncode != 0:
            return None, "pdf_onleesbaar"
        tekst = r.stdout.decode("utf-8", "replace")
        beelden = subprocess.run(["pdfimages", "-list", str(pad)], capture_output=True, timeout=60)
        if beelden.returncode != 0:
            return None, "pdf_beelden_onbekend"
        for regel in beelden.stdout.decode("utf-8", "replace").splitlines()[2:]:
            kol = regel.split()
            if len(kol) > 4 and kol[3].isdigit() and kol[4].isdigit():
                breed, hoog = int(kol[3]), int(kol[4])
                if min(breed, hoog) >= BEELD_MIN_ZIJDE or breed * hoog >= BEELD_MAX_OPPERVLAK:
                    return None, "pdf_scanbeeld"
        bijlagen_in_pdf = subprocess.run(["pdfdetach", "-list", str(pad)], capture_output=True, timeout=60)
        if bijlagen_in_pdf.returncode != 0:
            return None, "pdf_ingesloten_onbekend"
        eerste = (bijlagen_in_pdf.stdout.decode("utf-8", "replace").splitlines() or ["0"])[0]
        if not eerste.strip().startswith("0 "):
            return None, "pdf_ingesloten_bestanden"
        return tekst, None
    finally:
        try:
            os.unlink(pad)
        except OSError:
            pass


# ---- docx/xlsx: alleen bekende onderdelen (review MOET 3) -----------------------------------------------------------
# Wat de tekstextractie niet dekt, mag niet ongezien mee: een draaitabel-cache, externe koppeling, altChunk, ingesloten
# object, customXml, gegevensverbinding of macro bevat gegevens die Qwen nooit zou zien. Daarom een witte lijst: elk
# onderdeel is óf een tekstdeel (alle tekstknopen gaan mee naar Qwen), óf een vormdeel zonder tekst, óf beeld (grens
# hieronder). Al het andere = weigering. Uit tekstdelen gaat ALLES mee: ook voetnoten, eindnoten, opmerkingen,
# verwijderde tekst (w:delText), veldcodes, alt-tekst, bladnamen, kopteksten, documenteigenschappen (titel, onderwerp,
# auteur), grafiekwaarden en externe hyperlinkadressen.
OFFICE_WEIGER = (
    (r"(^|/)embeddings/|(^|/)activeX/|\.ole$|oleObject", "office_ingesloten"),
    (r"vbaProject|vbaData|\.bas$|(^|/)macrosheets/|(^|/)dialogsheets/", "office_macro"),
    (r"^xl/pivot(Cache|Tables)/", "office_draaitabel"),
    (r"^xl/externalLinks/", "office_externe_koppeling"),
    (r"^xl/(connections\.xml|queryTables/|model/)|(^|/)slicer|(^|/)timeline", "office_gegevensverbinding"),
    (r"(^|/)afchunk|altchunk", "office_altchunk"),
    (r"^customXml/", "office_customxml"),
    (r"^word/glossary/", "office_bouwstenen"),
)
_GEMEEN_TEKST = (r"docProps/(core|app|custom)\.xml",)
_GEMEEN_VORM = (r"\[Content_Types\]\.xml", r"(.+/)?_rels/[^/]*\.rels", r"docProps/thumbnail\.(jpeg|jpg|png|emf|wmf)")
OFFICE_DELEN = {
    "docx": {
        "tekst": _GEMEEN_TEKST + (
            r"word/document\d*\.xml", r"word/(header|footer)\d+\.xml", r"word/(footnotes|endnotes)\.xml",
            r"word/comments(Extended|Ids|Extensible)?\.xml", r"word/people\.xml", r"word/charts/chart\d+\.xml",
            r"word/diagrams/(data|drawing)\d+\.xml"),
        "vorm": _GEMEEN_VORM + (
            r"word/(styles|stylesWithEffects|settings|webSettings|fontTable|numbering)\.xml", r"word/theme/theme\d+\.xml",
            r"word/charts/(style|colors)\d+\.xml", r"word/diagrams/(layout|quickStyle|colors)\d+\.xml",
            r"word/printerSettings/printerSettings\d+\.bin", r"word/fonts/font\d+\.odttf"),
        "beeld": (r"word/media/[^/]+",),
    },
    "xlsx": {
        "tekst": _GEMEEN_TEKST + (
            r"xl/workbook\.xml", r"xl/sharedStrings\.xml", r"xl/worksheets/sheet\d+\.xml", r"xl/comments\d+\.xml",
            r"xl/threadedComments/threadedComment\d+\.xml", r"xl/persons/person\d*\.xml", r"xl/drawings/drawing\d+\.xml",
            r"xl/drawings/vmlDrawing\d+\.vml", r"xl/charts/chart\d+\.xml", r"xl/tables/table\d+\.xml"),
        "vorm": _GEMEEN_VORM + (
            r"xl/styles\.xml", r"xl/theme/theme\d+\.xml", r"xl/calcChain\.xml", r"xl/metadata\.xml",
            r"xl/charts/(style|colors)\d+\.xml", r"xl/chartsheets/sheet\d+\.xml",
            r"xl/printerSettings/printerSettings\d+\.bin"),
        "beeld": (r"xl/media/[^/]+",),
    },
}
# Attributen met tekst die een mens schreef (bladnaam, alt-tekst, auteur, namen in tabellen en personenlijsten).
_TEKST_ATTR = re.compile(r'\s(?:[A-Za-z0-9]+:)?(name|descr|title|author|displayName|userId|initials)="([^"]*)"')
_BLOK_EINDE = re.compile(r"</(?:[A-Za-z0-9]+:)?(p|tr|row|si|comment|threadedComment|txbxContent|ser|pt|definedName)>")
_CEL_EINDE = re.compile(r"</(?:[A-Za-z0-9]+:)?(tc|c|tab|cfRule)>|<(?:[A-Za-z0-9]+:)?(tab|br|cr)\s*/>")


def _deel_soort(naam, soort):
    for patroon, reden in OFFICE_WEIGER:
        if re.search(patroon, naam, re.I):
            return "weiger", reden
    for rol in ("tekst", "vorm", "beeld"):
        if any(re.fullmatch(p, naam) for p in OFFICE_DELEN[soort][rol]):
            return rol, None
    return "weiger", "office_onbekend_deel"


def _xml_tekst(ruw):
    """Alle tekstknopen (en tekstattributen) van één XML-deel, in volgorde. None = niet te lezen."""
    if ruw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        xml = ruw.decode("utf-16", "replace")
    else:
        xml = ruw.decode("utf-8", "replace")
    if re.search(r"<!(DOCTYPE|ENTITY)", xml, re.I):
        return None
    xml = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
    xml = re.sub(r"<!\[CDATA\[(.*?)\]\]>", lambda m: html.escape(m.group(1)), xml, flags=re.S)
    xml = re.sub(r"<\?.*?\?>", "", xml, flags=re.S)
    xml = _BLOK_EINDE.sub(lambda m: m.group(0) + "\n", xml)
    xml = _CEL_EINDE.sub(lambda m: m.group(0) + "\t", xml)
    delen = []
    for m in re.finditer(r"<[^>]*>|[^<]+", xml):
        stuk = m.group(0)
        if stuk.startswith("<"):
            delen.extend(" " + html.unescape(w) + " " for _, w in _TEKST_ATTR.findall(stuk) if w.strip())
        else:
            delen.append(html.unescape(stuk))
    return "".join(delen)


def _rels_extern(ruw):
    """Externe doelen in een .rels (hyperlinks, ook mailto:) en of er een altChunk aan hangt."""
    xml = ruw.decode("utf-8", "replace")
    altchunk = bool(re.search(r'Type="[^"]*/aFChunk"', xml, re.I))
    doelen = [html.unescape(m.group(1)) for m in re.finditer(r'<Relationship\b[^>]*?Target="([^"]*)"[^>]*TargetMode="External"', xml)]
    doelen += [html.unescape(m.group(1)) for m in re.finditer(r'<Relationship\b[^>]*?TargetMode="External"[^>]*Target="([^"]*)"', xml)]
    return altchunk, doelen


def _office(b, soort):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        infos = z.infolist()
        if len(infos) > MAX_OFFICE_DELEN or sum(i.file_size for i in infos) > MAX_OFFICE_TOTAAL:
            return None, "office_te_groot"
        rollen = {}
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > 50 * 1024 * 1024:
                return None, "office_te_groot"
            if info.flag_bits & 0x1:
                return None, "office_versleuteld"
            rol, reden = _deel_soort(info.filename, soort)
            if rol == "weiger":
                return None, reden
            if rol == "beeld" or info.filename.startswith("docProps/thumbnail"):
                if info.file_size > MAX_MEDIA_BYTES:
                    return None, "office_foto"
            rollen[info.filename] = rol
        if "[Content_Types].xml" not in rollen:
            return None, "office_onleesbaar_deel"
        if re.search(r"macroEnabled|vbaProject", z.read("[Content_Types].xml").decode("utf-8", "replace"), re.I):
            return None, "office_macro"
        hoofd = "word/document.xml" if soort == "docx" else "xl/workbook.xml"
        volgorde = [hoofd] + sorted(n for n, r in rollen.items() if r == "tekst" and n != hoofd)
        if soort == "xlsx":
            # bladen in nummervolgorde direct na de werkmap, dan de gedeelde teksten
            bladen = sorted((n for n in volgorde if n.startswith("xl/worksheets/")), key=lambda n: int(re.sub(r"\D", "", n) or 0))
            rest = [n for n in volgorde if n != hoofd and n not in bladen]
            volgorde = [hoofd] + bladen + rest
        delen = []
        for n in volgorde:
            t = _xml_tekst(z.read(n))
            if t is None:
                return None, "office_onleesbaar_deel"
            delen.append(t)
        for n, r in rollen.items():
            if n.endswith(".rels"):
                altchunk, doelen = _rels_extern(z.read(n))
                if altchunk:
                    return None, "office_altchunk"
                delen.extend(doelen)
        tekst = "\n".join(re.sub(r"[ \t]{2,}", " ", d).strip() for d in delen if d and d.strip())
        return tekst, None


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
    except (subprocess.SubprocessError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, OSError, KeyError, ValueError,
            RuntimeError, NotImplementedError, EOFError):
        return _weiger("onleesbaar", soort)
    if js_lengte(tekst) > MAX_TEKST:
        return _weiger("te_lang", soort)   # n8n weigert > 30.000 tekens; afkappen zou een deel ongezien laten
    return {"door": True, "reden": "tekstlaag", "soort": soort, "tekst": tekst,
            "sha256": hashlib.sha256(inhoud).hexdigest()}
