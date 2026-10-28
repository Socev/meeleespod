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
# veld tekent hij ook niet. Daarom: een pdf met een formulier of met een annotatie anders dan een link gaat niet door.
# Twee lagen, want één parser kan anders lezen dan poppler (review B3b MOET 3):
#  1. pypdf (strikt; per object soepel als strikt struikelt): elke /Annots op elke pagina moet bewezen een /Link zijn;
#     geen /AcroForm met velden, geen veld (/FT), geen ingesloten bestanden, niet versleuteld; elk object uit de xref
#     (ook uit objectstromen) wordt nagelopen.
#  2. een ruwe scan van de bytes plus elke objectstroom (ook zonder /Type, ook met een filterketen van Flate): PDF-
#     witruimte (NUL), commentaar tussen naam en waarde, #xx-namen en een indirecte /Subtype worden meegenomen.
# Wat een mens in de viewer ziet maar niet op de pagina staat (titel, onderwerp, trefwoorden, bladwijzers, XMP, de
# omschrijving van een link) gaat als extra tekst mee naar Qwen.
PDF_ANNOT_SOORTEN = frozenset({
    "Text", "FreeText", "Line", "Square", "Circle", "Polygon", "PolyLine", "Highlight", "Underline", "Squiggly",
    "StrikeOut", "Stamp", "Caret", "Ink", "Popup", "FileAttachment", "Sound", "Movie", "Widget", "Screen",
    "PrinterMark", "TrapNet", "Watermark", "3D", "Redact", "RichMedia", "Projection"})
_PDF_WIT = rb"(?:[\x00\t\n\x0c\r ]|%[^\r\n]*[\r\n])*"
_PDF_NAAM_HEX = re.compile(rb"#([0-9A-Fa-f]{2})")
_PDF_STROOM = re.compile(rb"\bstream\r?\n")


def _pdf_naam_normaal(b):
    """PDF-namen mogen #xx-codes bevatten (/Sub#74ype); voor het zoeken alles terugvertalen (vals alarm = weigering)."""
    return _PDF_NAAM_HEX.sub(lambda m: bytes([int(m.group(1), 16)]), b)


def _pdf_ruwe_scan(b):
    """Laag 2. Geeft None of een redencode."""
    delen, totaal = [b], 0
    for m in _PDF_STROOM.finditer(b):
        begin = b.rfind(b"obj", 0, m.start())
        kop = _pdf_naam_normaal(b[begin if begin >= 0 else 0:m.start()])
        objstroom = re.search(rb"/ObjStm\b", kop) or (re.search(rb"/N\b", kop) and re.search(rb"/First\b", kop))
        if not objstroom:
            continue   # paginastromen, beelden, lettertypen: geen objecten
        eind = b.find(b"endstream", m.end())
        if eind < 0:
            return "pdf_structuur_onleesbaar"
        f = re.search(rb"/Filter" + _PDF_WIT + rb"(\[[^\]]*\]|/[A-Za-z0-9]+)", kop)
        filters = re.findall(rb"/([A-Za-z0-9]+)", f.group(1)) if f else []
        if any(x not in (b"FlateDecode", b"Fl") for x in filters):
            return "pdf_structuur_onleesbaar"
        if re.search(rb"/Predictor" + _PDF_WIT + rb"(\d+)", kop) and not re.search(rb"/Predictor" + _PDF_WIT + rb"1\b", kop):
            return "pdf_structuur_onleesbaar"
        data = b[m.end():eind]
        for _ in filters:
            rest = MAX_PDF_STROMEN - totaal
            if rest <= 0:
                return "pdf_structuur_te_groot"
            try:
                d = zlib.decompressobj()
                data = d.decompress(data, rest)
            except zlib.error:
                return "pdf_structuur_onleesbaar"
            if d.unconsumed_tail:
                return "pdf_structuur_te_groot"
        totaal += len(data)
        if totaal > MAX_PDF_STROMEN:
            return "pdf_structuur_te_groot"
        delen.append(data)
    alles = _pdf_naam_normaal(b"\n".join(delen))
    if re.search(rb"/Encrypt\b", alles):
        return "pdf_versleuteld"
    if re.search(rb"/(XFA|FT)\b", alles):
        return "pdf_formulier"   # een veld heeft altijd /FT (zelf of via de ouder); een leeg /AcroForm beoordeelt pypdf
    if re.search(rb"/(EmbeddedFiles|EmbeddedFile|RichMediaContent)\b", alles):
        return "pdf_ingesloten_bestanden"
    if re.search(rb"/Subtype" + _PDF_WIT + rb"\d+" + _PDF_WIT + rb"\d+" + _PDF_WIT + rb"R\b", alles):
        return "pdf_structuur_vreemd"   # een indirecte /Subtype is geldig maar ongebruikelijk: niet gokken
    for s in re.findall(rb"/Subtype" + _PDF_WIT + rb"/([A-Za-z0-9]+)", alles):
        if s.decode("latin-1") in PDF_ANNOT_SOORTEN:
            return "pdf_opmerkingen"
    return None


def _pdf_tekst(w):
    try:
        return str(w).strip()
    except Exception:
        return ""


def _pdf_pypdf(b):
    """Laag 1. Geeft (redencode of None, extra tekst)."""
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject
    try:
        r = PdfReader(io.BytesIO(b), strict=True)
        if r.is_encrypted:
            return "pdf_versleuteld", ""
        root = r.trailer["/Root"].get_object()
        if "/AcroForm" in root:
            af = root["/AcroForm"].get_object()
            velden = af.get("/Fields") if isinstance(af, DictionaryObject) else None
            velden = velden.get_object() if velden is not None else None
            # gemeten 28-10: veel gewone pdf's dragen een leeg /AcroForm; alleen met velden of XFA is het een formulier
            if not isinstance(af, DictionaryObject) or "/XFA" in af or (velden is not None and not isinstance(velden, ArrayObject)) \
                    or (velden is not None and len(velden) > 0):
                return "pdf_formulier", ""
        namen = root.get("/Names")
        if namen is not None and "/EmbeddedFiles" in namen.get_object():
            return "pdf_ingesloten_bestanden", ""
        extra = []
        for pagina in r.pages:
            annots = pagina.get("/Annots")
            if annots is None:
                continue
            annots = annots.get_object()
            if not isinstance(annots, ArrayObject):
                return "pdf_structuur_vreemd", ""
            for a in annots:
                a = a.get_object()
                if not isinstance(a, DictionaryObject) or isinstance(a.get("/Subtype"), IndirectObject) \
                        or a.get("/Subtype") != NameObject("/Link"):
                    return "pdf_opmerkingen", ""
                if a.get("/Contents"):
                    extra.append(_pdf_tekst(a["/Contents"]))
        # elk object in de xref (ook ongebruikte en die in objectstromen)
        nummers = {(n, 0) for n in r.xref_objStm}
        for gen, tabel in r.xref.items():
            nummers.update((n, gen) for n in tabel)
        los = None
        for num, gen in sorted(nummers):
            try:
                o = r.get_object(IndirectObject(num, gen, r))
            except Exception:
                # gemeten 28-10 op gewone pdf's: een vrij xref-item of een dubbele /Length laat de strikte lezer
                # struikelen; dan het object nog eens soepel lezen en dát beoordelen (niet te lezen = weigeren)
                try:
                    los = los or PdfReader(io.BytesIO(b), strict=False)
                    o = los.get_object(IndirectObject(num, gen, los))
                except Exception:
                    return "pdf_structuur_onleesbaar", ""
            if isinstance(o, DictionaryObject):
                sub = o.get("/Subtype")
                if isinstance(sub, IndirectObject) or "/FT" in o:
                    return "pdf_formulier" if "/FT" in o else "pdf_structuur_vreemd", ""
                if (o.get("/Type") == NameObject("/Annot") or (isinstance(sub, str) and sub.lstrip("/") in PDF_ANNOT_SOORTEN)) \
                        and sub != NameObject("/Link"):
                    return "pdf_opmerkingen", ""
        info = r.metadata or {}
        for k in ("/Title", "/Subject", "/Keywords", "/Author"):
            if info.get(k):
                extra.append(_pdf_tekst(info[k]))

        def bladwijzers(lijst):
            for x in lijst:
                if isinstance(x, list):
                    bladwijzers(x)
                else:
                    extra.append(_pdf_tekst(x.get("/Title", "")))
        bladwijzers(r.outline or [])
        meta = root.get("/Metadata")
        if meta is not None:
            t = _xml_tekst(meta.get_object().get_data(), {})
            if t is None:
                return "pdf_structuur_vreemd", ""
            extra.append(t)
        return None, "\n".join(x for x in extra if x)
    except Exception:
        return "pdf_structuur_onleesbaar", ""


def pdf_structuur(b):
    """Geeft (None | redencode, extra tekst voor Qwen)."""
    fout = _pdf_ruwe_scan(b)
    if fout:
        return fout, ""
    return _pdf_pypdf(b)


def _pdf(b):
    fout, extra = pdf_structuur(b)
    if fout:
        return None, fout, ""
    I.WERK.mkdir(parents=True, exist_ok=True, mode=0o700)
    pad = I.WERK / f"b-{secrets.token_hex(8)}.pdf"
    fd = os.open(pad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        info = subprocess.run(["pdfinfo", str(pad)], capture_output=True, timeout=60)
        if info.returncode != 0:
            return None, "pdf_onleesbaar", ""
        vorm = re.search(r"^Form:\s*(\S+)", info.stdout.decode("utf-8", "replace"), re.M)
        if not vorm or vorm.group(1).lower() not in ("none", "acroform"):
            return None, "pdf_formulier", ""   # XFA of onbekend; een AcroForm met velden weigert pdf_structuur al
        r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", "-layout", str(pad), "-"], capture_output=True,
                           timeout=60)
        if r.returncode != 0:
            return None, "pdf_onleesbaar", ""
        tekst = r.stdout.decode("utf-8", "replace")
        beelden = subprocess.run(["pdfimages", "-list", str(pad)], capture_output=True, timeout=60)
        if beelden.returncode != 0:
            return None, "pdf_beelden_onbekend", ""
        for regel in beelden.stdout.decode("utf-8", "replace").splitlines()[2:]:
            kol = regel.split()
            if len(kol) > 4 and kol[3].isdigit() and kol[4].isdigit():
                breed, hoog = int(kol[3]), int(kol[4])
                if min(breed, hoog) >= BEELD_MIN_ZIJDE or breed * hoog >= BEELD_MAX_OPPERVLAK:
                    return None, "pdf_scanbeeld", ""
        bijlagen_in_pdf = subprocess.run(["pdfdetach", "-list", str(pad)], capture_output=True, timeout=60)
        if bijlagen_in_pdf.returncode != 0:
            return None, "pdf_ingesloten_onbekend", ""
        eerste = (bijlagen_in_pdf.stdout.decode("utf-8", "replace").splitlines() or ["0"])[0]
        if not eerste.strip().startswith("0 "):
            return None, "pdf_ingesloten_bestanden", ""
        return tekst, None, extra
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
# Attributen: ALLE waarden gaan mee (review B3b ZOU 4: tooltip, fldSimple-instr, watermerk-textpath, docVar,
# gegevensvalidatie, listItem-displayText, alt-tekst met enkele aanhalingstekens …), ontdubbeld per bestand zodat de
# vele opmaakwaarden (left, Calibri, Heading1) maar één keer tellen. Weg: namespaces, getallen/hex/ja-nee en
# schema-adressen.
_ATTR = re.compile(r"""\s([A-Za-z_][\w.-]*(?::[\w.-]+)?)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ATTR_TECHNISCH = re.compile(
    r"^(?:-?[0-9a-fA-F.]+%?|true|false|on|off|none|auto|[A-Za-z]{1,2}|\{?[0-9A-Fa-f-]{8,}\}?|"
    r"\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?(?:\s+\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)*|"
    r"https?://(?:schemas\.(?:openxmlformats\.org|microsoft\.com)|purl\.org|www\.w3\.org)/\S*|"
    r"urn:schemas-microsoft-com:\S*|application/[\w.+-]+|image/[\w.+-]+|/[\w./\[\]-]+\.(?:xml|rels|vml|bin|png|jpe?g|gif|emf|wmf))$", re.I)
# Zuiver structurele attributen (celverwijzing, inhoudstype, opmaak) en de vaste lijst ingebouwde stijlnamen.
_ATTR_WEG = frozenset({"r", "ref", "sqref", "spans", "ContentType", "PartName", "Extension", "Ignorable", "space",
                       "encoding", "typeface", "script", "styleId", "patternType", "horizontal", "vertical", "state",
                       "t", "s", "cap", "Id", "id", "rsidR", "rsidRPr", "rsidRDefault", "rsidP", "rsidTr", "rsidSect",
                       "rsidDel", "paraId", "textId", "ascii", "hAnsi", "eastAsia", "cs", "panose", "pitch", "family",
                       "charset", "sig", "usb0", "usb1", "usb2", "usb3", "csb0", "csb1", "uiPriority", "fill", "color",
                       "themeColor", "rgb", "theme", "tint", "formatCode", "numFmtId", "fontId", "fillId", "borderId",
                       "xfId", "applyFont", "applyFill", "applyBorder", "applyAlignment", "applyNumberFormat"})
_ELEM_WEG = frozenset({"lsdException", "latentStyles", "font", "sig", "panose1", "altName", "family", "charset",
                       "pitch", "Default", "Override"})
_BLOK_EINDE = re.compile(r"</(?:[A-Za-z0-9]+:)?(p|tr|row|si|comment|threadedComment|txbxContent|ser|pt|definedName)>")
_CEL_EINDE = re.compile(r"</(?:[A-Za-z0-9]+:)?(tc|c|tab|cfRule)>|<(?:[A-Za-z0-9]+:)?(tab|br|cr)\s*/>")
# Relaties die inhoud binnenhalen die de tekstextractie niet dekt (review B3b MOET 2): altijd weigeren.
_REL_WEIGER = re.compile(r'Type\s*=\s*["\'][^"\']*/(aFChunk|oleObject|package|subDocument|frame|control|externalLink|'
                         r'externalLinkPath|pivotCacheDefinition|pivotTable|connections|queryTable|vbaProject|'
                         r'customXml|activeXControlBinary|slicer|slicerCache|timeline)["\']', re.I)


def _deel_soort(naam, soort):
    for patroon, reden in OFFICE_WEIGER:
        if re.search(patroon, naam, re.I):
            return "weiger", reden
    for rol in ("tekst", "vorm", "beeld"):
        if any(re.fullmatch(p, naam) for p in OFFICE_DELEN[soort][rol]):
            return rol, None
    return "weiger", "office_onbekend_deel"


def _xml_tekst(ruw, kenmerken):
    """Alle tekstknopen van één XML-deel, in volgorde; attribuutwaarden gaan (ontdubbeld) in `kenmerken`.
    None = niet te lezen (DOCTYPE/ENTITY)."""
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
            el = re.match(r"</?\s*(?:[\w.-]+:)?([\w.-]+)", stuk)
            if el and el.group(1) in _ELEM_WEG:
                continue
            for naam, w1, w2 in _ATTR.findall(stuk):
                w = html.unescape(w1 or w2).strip()
                kort = naam.split(":")[-1]
                if naam.lower().startswith("xmlns") or kort in _ATTR_WEG or not w or _ATTR_TECHNISCH.match(w):
                    continue
                kenmerken.setdefault(w, None)
        else:
            delen.append(html.unescape(stuk))
    return "".join(delen)


def _is_xml(ruw):
    """XML zodra er na willekeurig veel witruimte een '<' komt (review B3b, tweede ronde: ook bij een 'binaire' naam)."""
    return ruw.lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n").startswith(b"<")


# Een binair vormdeel moet ook in [Content_Types] als dat soort staan; anders kan het via _rels een koptekst zijn.
_BINAIR_TYPE = {".bin": "printersettings", ".odttf": "obfuscatedfont"}


def _inhoudstypen(ct_xml):
    """(Overrides {deelnaam: type}, Defaults {extensie: type}) uit [Content_Types].xml, alles in kleine letters."""
    over = {html.unescape(a).lstrip("/").lower(): html.unescape(b).lower() for a, b in re.findall(
        r'<Override\b[^>]*?PartName\s*=\s*["\']([^"\']*)["\'][^>]*?ContentType\s*=\s*["\']([^"\']*)["\']', ct_xml)}
    over.update({html.unescape(a).lstrip("/").lower(): html.unescape(b).lower() for b, a in re.findall(
        r'<Override\b[^>]*?ContentType\s*=\s*["\']([^"\']*)["\'][^>]*?PartName\s*=\s*["\']([^"\']*)["\']', ct_xml)})
    std = {a.lower(): b.lower() for a, b in re.findall(
        r'<Default\b[^>]*?Extension\s*=\s*["\']([^"\']*)["\'][^>]*?ContentType\s*=\s*["\']([^"\']*)["\']', ct_xml)}
    std.update({a.lower(): b.lower() for b, a in re.findall(
        r'<Default\b[^>]*?ContentType\s*=\s*["\']([^"\']*)["\'][^>]*?Extension\s*=\s*["\']([^"\']*)["\']', ct_xml)})
    return over, std


def _beeld_maat(b):
    """(breed, hoog) van png/jpeg/gif uit de kop; None = geen van die drie of onleesbaar (dan weigeren)."""
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24 and b[12:16] == b"IHDR":
        return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    if b[:6] in (b"GIF87a", b"GIF89a") and len(b) >= 10:
        return int.from_bytes(b[6:8], "little"), int.from_bytes(b[8:10], "little")
    if b[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                return None
            soort = b[i + 1]
            if soort in (0xD8, 0x01) or 0xD0 <= soort <= 0xD7:
                i += 2
                continue
            lengte = int.from_bytes(b[i + 2:i + 4], "big")
            if 0xC0 <= soort <= 0xCF and soort not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
            i += 2 + lengte
    return None


def _office(b, soort):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        infos = z.infolist()
        if len(infos) > MAX_OFFICE_DELEN or sum(i.file_size for i in infos) > MAX_OFFICE_TOTAAL:
            return None, "office_te_groot"
        if len({i.filename.lower() for i in infos}) != len(infos):
            return None, "office_dubbele_delen"   # zipfile leest het laatste exemplaar; Word misschien het eerste
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
            if info.filename.startswith("docProps/thumbnail"):
                rol = "beeld"
            rollen[info.filename] = rol
        if "[Content_Types].xml" not in rollen:
            return None, "office_onleesbaar_deel"
        ct_xml = z.read("[Content_Types].xml").decode("utf-8", "replace")
        if re.search(r"macroEnabled|vbaProject", ct_xml, re.I):
            return None, "office_macro"
        over, std = _inhoudstypen(ct_xml)
        for n in rollen:
            ext = "." + n.rsplit(".", 1)[-1].lower() if "." in n else ""
            if ext in _BINAIR_TYPE:
                ct = over.get(n.lower()) or std.get(ext[1:], "")
                if _BINAIR_TYPE[ext] not in ct.replace("-", ""):
                    return None, "office_inhoudstype_vreemd"
        # Beeld: alleen png/jpeg/gif (op inhoud), klein (review B3b MOET 1: een emf/svg/wmf kan tekst dragen, bv. een
        # als plaatje geplakte tabel; die tekst ziet Qwen niet).
        for n, r in rollen.items():
            if r != "beeld":
                continue
            if z.getinfo(n).file_size > MAX_MEDIA_BYTES:
                return None, "office_foto"
            maat = _beeld_maat(z.read(n))
            if maat is None:
                return None, "office_beeld_vreemd"
            if min(maat) >= BEELD_MIN_ZIJDE or maat[0] * maat[1] >= BEELD_MAX_OPPERVLAK:
                return None, "office_foto"
        # Tekst uit ELK XML-deel, ook de vormdelen (review B3b MOET 2: welk deel Word als hoofddocument of koptekst
        # gebruikt, bepalen _rels en [Content_Types], niet de naam; zo ziet Qwen alles wat Word zou kunnen tonen).
        hoofd = "word/document.xml" if soort == "docx" else "xl/workbook.xml"
        if soort == "xlsx":
            bladen = sorted((n for n in rollen if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                            key=lambda n: int(re.sub(r"\D", "", n) or 0))
        else:
            bladen = []
        eerst = [hoofd] + bladen
        volgorde = eerst + sorted(n for n, r in rollen.items() if r == "tekst" and n not in eerst) \
            + sorted(n for n, r in rollen.items() if r == "vorm" and n not in eerst)
        delen, kenmerken = [], {}
        for n in volgorde:
            ruw = z.read(n)
            if n.endswith(".rels"):
                xml = ruw.decode("utf-8", "replace")
                if _REL_WEIGER.search(xml):
                    return None, "office_relatie_niet_toegestaan"
            if not (_is_xml(ruw) or n.endswith((".xml", ".rels", ".vml"))):
                continue   # binaire vormdelen (printerinstellingen, lettertypen): geen tekst
            t = _xml_tekst(ruw, kenmerken)
            if t is None:
                return None, "office_onleesbaar_deel"
            delen.append(t)
        if kenmerken:
            delen.append("[kenmerken] " + " | ".join(kenmerken))
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
            tekst, fout, extra = _pdf(bytes(inhoud))
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
            if extra:
                tekst = tekst + "\n" + extra   # na de paginatoets: titel, bladwijzers, XMP, linkomschrijving
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
