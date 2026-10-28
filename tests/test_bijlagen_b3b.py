"""B3b (wv1751): de bijlagepunten uit de Opus-review op de n8n-diff, pod-kant. Alleen verzonnen bestanden."""
import io
import zipfile

import pytest

from meeleespod import bijlagen
from meeleespod.toetsdata import pdf_met_annotatie, pdf_met_objectstroom, pdf_met_tekst

TEKST = "Notulen werkoverleg over het rooster van de balie. " * 10
CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>'
DOC = ('<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>' + TEKST + '</w:t></w:r></w:p>{extra}</w:body></w:document>')


def _zip(delen):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for naam, inhoud in delen.items():
            z.writestr(naam, inhoud)
    return b.getvalue()


def _docx(extra_delen=None, extra_body="", ct=CT):
    delen = {"[Content_Types].xml": ct, "_rels/.rels": "<Relationships/>", "word/document.xml": DOC.format(extra=extra_body),
             "word/_rels/document.xml.rels": "<Relationships/>", "word/styles.xml": "<w:styles/>"}
    delen.update(extra_delen or {})
    return _zip(delen)


def _xlsx(extra_delen=None):
    delen = {"[Content_Types].xml": CT, "_rels/.rels": "<Relationships/>",
             "xl/workbook.xml": '<workbook><sheets><sheet name="Verzonnen rooster" sheetId="1"/>'
                                '<sheet name="Verborgen blad" state="hidden" sheetId="2"/></sheets></workbook>',
             "xl/sharedStrings.xml": "<sst><si><t>" + TEKST + "</t></si></sst>",
             "xl/worksheets/sheet1.xml": "<worksheet><sheetData><row><c t='s'><v>0</v></c><c><f>SUM(A1:A3)</f><v>6</v></c></row></sheetData></worksheet>",
             "xl/styles.xml": "<styleSheet/>"}
    delen.update(extra_delen or {})
    return _zip(delen)


# ---- MOET 1: terugval-oordeel telt niet -------------------------------------------------------------------------------
def test_terugval_is_geen_uitdrukkelijk_oordeel():
    assert bijlagen.mag_bijlagen({"klasse": "twijfel"})
    assert bijlagen.mag_bijlagen({"klasse": "zakelijk", "terugval": False})
    for t in (True, "true", 1, "ja", {}):
        assert not bijlagen.mag_bijlagen({"klasse": "twijfel", "terugval": t}), t


# ---- MOET 3: docx/xlsx --------------------------------------------------------------------------------------------------
def test_docx_alle_tekstdelen_mee():
    d = _docx({
        "word/footnotes.xml": "<w:footnotes><w:footnote><w:p><w:r><w:t>VOETNOOT-verzonnen</w:t></w:r></w:p></w:footnote></w:footnotes>",
        "word/endnotes.xml": "<w:endnotes><w:endnote><w:p><w:r><w:t>EINDNOOT-verzonnen</w:t></w:r></w:p></w:endnote></w:endnotes>",
        "word/comments.xml": '<w:comments><w:comment w:author="Auteur Verzonnen"><w:p><w:r><w:t>OPMERKING-verzonnen</w:t></w:r></w:p></w:comment></w:comments>',
        "docProps/core.xml": "<cp:coreProperties><dc:title>TITEL-verzonnen</dc:title><dc:subject>ONDERWERP-verzonnen</dc:subject>"
                             "<dc:creator>MAKER-verzonnen</dc:creator></cp:coreProperties>",
        "word/header1.xml": "<w:hdr><w:p><w:r><w:t>KOPTEKST-verzonnen</w:t></w:r></w:p></w:hdr>",
        "word/_rels/document.xml.rels": '<Relationships><Relationship Id="r1" Type="x/hyperlink" Target="mailto:iemand@voorbeeld.nl" TargetMode="External"/></Relationships>',
    }, extra_body='<w:p><w:del><w:r><w:delText>WEGGEHAALD-verzonnen</w:delText></w:r></w:del>'
                  '<w:r><w:instrText> MERGEFIELD VELDCODE-verzonnen </w:instrText></w:r></w:p>'
                  '<wp:docPr id="1" name="Afbeelding 1" descr="ALTTEKST-verzonnen"/>')
    r = bijlagen.beoordeel("n.docx", d)
    assert r["door"] is True, r["reden"]
    for woord in ("VOETNOOT", "EINDNOOT", "OPMERKING", "Auteur Verzonnen", "TITEL", "ONDERWERP", "MAKER", "KOPTEKST",
                  "WEGGEHAALD", "VELDCODE", "ALTTEKST", "iemand@voorbeeld.nl"):
        assert woord in r["tekst"], woord
    assert "Notulen werkoverleg" in r["tekst"].split("\n")[0], "hoofdtekst eerst"


@pytest.mark.parametrize("naam,reden", [
    ("word/afchunk.mht", "office_altchunk"),
    ("word/embeddings/oleObject1.bin", "office_ingesloten"),
    ("word/vbaProject.bin", "office_macro"),
    ("customXml/item1.xml", "office_customxml"),
    ("word/glossary/document.xml", "office_bouwstenen"),
    ("word/iets-nieuws.xml", "office_onbekend_deel"),
])
def test_docx_onbekende_of_verborgen_delen_geweigerd(naam, reden):
    r = bijlagen.beoordeel("n.docx", _docx({naam: "<x>verzonnen</x>"}))
    assert r["door"] is False and r["reden"] == reden


def test_docx_altchunk_via_relatie_en_macro_via_inhoudstype():
    rels = '<Relationships><Relationship Id="r9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="chunk.htm"/></Relationships>'
    assert bijlagen.beoordeel("n.docx", _docx({"word/_rels/document.xml.rels": rels}))["reden"] == "office_altchunk"
    ct = CT.replace("</Types>", '<Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>')
    assert bijlagen.beoordeel("n.docm", _docx(ct=ct))["reden"] == "office_macro"


def test_docx_doctype_geweigerd():
    d = _docx({"word/footnotes.xml": '<!DOCTYPE x [<!ENTITY a "b">]><w:footnotes/>'})
    assert bijlagen.beoordeel("n.docx", d)["reden"] == "office_onleesbaar_deel"


def test_xlsx_bladnamen_formules_opmerkingen_mee():
    x = _xlsx({"xl/comments1.xml": "<comments><authors><author>Auteur X</author></authors><commentList><comment><text><t>CELNOTITIE-verzonnen</t></text></comment></commentList></comments>",
               "docProps/core.xml": "<cp:coreProperties><dc:title>WERKMAPTITEL</dc:title></cp:coreProperties>"})
    r = bijlagen.beoordeel("r.xlsx", x)
    assert r["door"] is True, r["reden"]
    for woord in ("Verzonnen rooster", "Verborgen blad", "SUM(A1:A3)", "CELNOTITIE", "WERKMAPTITEL", "Notulen werkoverleg"):
        assert woord in r["tekst"], woord


@pytest.mark.parametrize("naam,reden", [
    ("xl/pivotCache/pivotCacheDefinition1.xml", "office_draaitabel"),
    ("xl/pivotCache/pivotCacheRecords1.xml", "office_draaitabel"),
    ("xl/pivotTables/pivotTable1.xml", "office_draaitabel"),
    ("xl/externalLinks/externalLink1.xml", "office_externe_koppeling"),
    ("xl/connections.xml", "office_gegevensverbinding"),
    ("xl/queryTables/queryTable1.xml", "office_gegevensverbinding"),
    ("xl/model/item.data", "office_gegevensverbinding"),
    ("xl/embeddings/Microsoft_Word_Document.docx", "office_ingesloten"),
    ("xl/vbaProject.bin", "office_macro"),
    ("xl/macrosheets/sheet1.xml", "office_macro"),
    ("customXml/item1.xml", "office_customxml"),
    ("xl/richData/rdrichvalue.xml", "office_onbekend_deel"),
])
def test_xlsx_verborgen_gegevens_geweigerd(naam, reden):
    r = bijlagen.beoordeel("r.xlsx", _xlsx({naam: "<x>verzonnen</x>"}))
    assert r["door"] is False and r["reden"] == reden


def test_office_versleuteld_of_bom():
    import zipfile as zf
    b = io.BytesIO()
    with zf.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("word/document.xml", DOC.format(extra=""))
    data = bytearray(b.getvalue())
    # versleuteld-vlag (bit 0) op het eerste deel, in zowel lokale kop als centrale map
    data[6] |= 1
    cd = data.find(b"PK\x01\x02")
    data[cd + 8] |= 1
    assert bijlagen.beoordeel("v.docx", bytes(data))["door"] is False


# ---- MOET 3: pdf --------------------------------------------------------------------------------------------------------
def test_pdf_formulier_en_notitie_geweigerd():
    assert bijlagen.beoordeel("a.pdf", pdf_met_tekst())["door"] is True
    r = bijlagen.beoordeel("f.pdf", pdf_met_annotatie(b"Widget", formulier=True))
    assert r["door"] is False and r["reden"] == "pdf_formulier"
    for soort in (b"Text", b"FreeText", b"Highlight", b"Popup", b"FileAttachment"):
        r = bijlagen.beoordeel("n.pdf", pdf_met_annotatie(soort))
        assert r["door"] is False and r["reden"] == "pdf_opmerkingen", soort


def test_pdf_alleen_link_mag():
    r = bijlagen.beoordeel("l.pdf", pdf_met_annotatie(b"Link"))
    assert r["door"] is True, r["reden"]


def test_pdf_structuur_objectstroom_en_namen():
    assert bijlagen.pdf_structuur(pdf_met_objectstroom(b"<< /Subtype /Text /Contents (x) >>")) == "pdf_opmerkingen"
    assert bijlagen.pdf_structuur(pdf_met_objectstroom(b"<< /Sub#74ype /T#65xt >>")) == "pdf_opmerkingen", "#xx-namen"
    assert bijlagen.pdf_structuur(pdf_met_objectstroom(b"<< /AcroForm << >> >>")) == "pdf_formulier"
    assert bijlagen.pdf_structuur(pdf_met_objectstroom(b"<< /Subtype /Link >>")) is None
    assert bijlagen.pdf_structuur(pdf_met_objectstroom(b"<< >>", b"/LZWDecode")) == "pdf_structuur_onleesbaar"
    assert bijlagen.pdf_structuur(pdf_met_objectstroom(b"<< >>", parms=b" /DecodeParms << /Predictor 12 >>")) == "pdf_structuur_onleesbaar"
    kapot = pdf_met_objectstroom(b"<< >>").replace(b"stream\n", b"stream\nKAPOT", 1)
    assert bijlagen.pdf_structuur(kapot) == "pdf_structuur_onleesbaar"
    assert bijlagen.pdf_structuur(pdf_met_tekst().replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 9 0 R")) == "pdf_versleuteld"


# ---- ZOU 3: grenzen gelijk aan n8n ----------------------------------------------------------------------------------------
def test_grenzen_gelijk_aan_n8n():
    assert bijlagen.MAX_BYTES == 11 * 1024 * 1024 and bijlagen.MAX_TEKST == 30_000
    assert bijlagen.beoordeel("g.pdf", b"%PDF-" + b"0" * (11 * 1024 * 1024))["reden"] == "te_groot"
    lang = _docx({"word/footnotes.xml": "<w:footnotes><w:p><w:r><w:t>" + "a" * 30_100 + "</w:t></w:r></w:p></w:footnotes>"})
    r = bijlagen.beoordeel("l.docx", lang)
    assert r["door"] is False and r["reden"] == "te_lang", "langer dan n8n aanneemt: niet versturen, niet afkappen"


def test_js_lengte_telt_zoals_n8n():
    assert bijlagen.js_lengte("  abc\x00  ") == 3
    assert bijlagen.js_lengte("\U0001F600") == 2, "buiten het BMP telt JavaScript twee eenheden"
