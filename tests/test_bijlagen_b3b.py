"""B3b (wv1751): de bijlagepunten uit de Opus-review op de n8n-diff, pod-kant. Alleen verzonnen bestanden."""
import io
import zipfile

import pytest

from meeleespod import bijlagen
from meeleespod.toetsdata import pdf_met_annot_ruw, pdf_met_annotatie, pdf_met_objectstroom, pdf_met_tekst, png_kop

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
    assert bijlagen.beoordeel("n.docx", _docx({"word/_rels/document.xml.rels": rels}))["reden"] == "office_relatie_niet_toegestaan"
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


def _pdf_met_catalogus(extra):
    from meeleespod.toetsdata import _pdf, _stroom, _tekstpagina
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R " + extra + b" >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
                 _stroom(_tekstpagina()), b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"])


def test_pdf_leeg_acroform_mag_met_velden_niet():
    r = bijlagen.beoordeel("leeg.pdf", _pdf_met_catalogus(b"/AcroForm << /Fields [] >>"))
    assert r["door"] is True, ("gemeten: gewone pdf's dragen vaak een leeg AcroForm", r["reden"])
    assert bijlagen.beoordeel("xfa.pdf", _pdf_met_catalogus(b"/AcroForm << /Fields [] /XFA 9 0 R >>"))["reden"] == "pdf_formulier"


def test_pdf_alleen_link_mag():
    r = bijlagen.beoordeel("l.pdf", pdf_met_annotatie(b"Link"))
    assert r["door"] is True, r["reden"]


def test_pdf_ruwe_scan_objectstroom_en_namen():
    scan = bijlagen._pdf_ruwe_scan
    assert scan(pdf_met_objectstroom(b"<< /Subtype /Text /Contents (x) >>")) == "pdf_opmerkingen"
    assert scan(pdf_met_objectstroom(b"<< /Sub#74ype /T#65xt >>")) == "pdf_opmerkingen", "#xx-namen"
    assert scan(pdf_met_objectstroom(b"<< /FT /Tx /T (veld) /V (verzonnen) >>")) == "pdf_formulier"
    assert scan(pdf_met_objectstroom(b"<< /Subtype /Link >>")) is None
    assert scan(pdf_met_objectstroom(b"<< >>", b"/LZWDecode")) == "pdf_structuur_onleesbaar"
    assert scan(pdf_met_objectstroom(b"<< >>", parms=b" /DecodeParms << /Predictor 12 >>")) == "pdf_structuur_onleesbaar"
    kapot = pdf_met_objectstroom(b"<< >>").replace(b"stream\n", b"stream\nKAPOT", 1)
    assert scan(kapot) == "pdf_structuur_onleesbaar"
    assert scan(pdf_met_tekst().replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 9 0 R")) == "pdf_versleuteld"
    # review B3b: objectstroom zonder /Type, met dubbele Flate, met een kop > 4096 bytes
    import zlib
    binnen = b"<< /Subtype /Text /Contents (verzonnen) >>"
    zonder_type = pdf_met_objectstroom(binnen).replace(b"/Type /ObjStm ", b"")
    assert scan(zonder_type) == "pdf_opmerkingen"
    dubbel = zlib.compress(zlib.compress(binnen))
    p = (b"%PDF-1.5\n7 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter [/FlateDecode /FlateDecode] /Length "
         + str(len(dubbel)).encode() + b" >>\nstream\n" + dubbel + b"\nendstream\nendobj\n")
    assert scan(p) == "pdf_opmerkingen"
    groot = pdf_met_objectstroom(binnen).replace(b"/N 1 ", b"/N 1 /Opvulling (" + b"x" * 5000 + b") ")
    assert scan(groot) == "pdf_opmerkingen"


def test_pdf_omzeilingen_uit_de_review():
    """Review B3b MOET 3: NUL als witruimte, commentaar tussen naam en waarde, indirecte /Subtype."""
    notitie = b" /Rect [50 500 70 520] /Contents (Verzonnen notitie) /P 3 0 R >>"
    gevallen = {
        "nul": pdf_met_annot_ruw(b"<< /Type /Annot /Subtype\x00/Text" + notitie),
        "commentaar": pdf_met_annot_ruw(b"<< /Type /Annot /Subtype %x\n/Text" + notitie),
        "indirect": pdf_met_annot_ruw(b"<< /Type /Annot /Subtype 7 0 R" + notitie, extra=[b"/Text"]),
        "zonder_subtype": pdf_met_annot_ruw(b"<< /Type /Annot" + notitie),
    }
    for naam, pdf in gevallen.items():
        r = bijlagen.beoordeel(naam + ".pdf", pdf)
        assert r["door"] is False and r["reden"].startswith("pdf_"), (naam, r["reden"])


def test_pdf_pypdf_laag_alleen_link_en_extra_tekst():
    link = b"<< /Type /Annot /Subtype /Link /Rect [50 600 250 620] /Contents (LINKUITLEG-verzonnen) >>"
    pdf = pdf_met_annot_ruw(link, extra=[b"<< /Title (PDFTITEL-verzonnen) /Subject (PDFONDERWERP) >>"], info=b" /Info 7 0 R")
    r = bijlagen.beoordeel("l.pdf", pdf)
    assert r["door"] is True, r["reden"]
    assert "LINKUITLEG" in r["tekst"] and "PDFTITEL" in r["tekst"] and "PDFONDERWERP" in r["tekst"]
    # een niet-link die alleen pypdf ziet (Subtype via een naam die de ruwe scan niet kent, bv. een eigen soort)
    vreemd = pdf_met_annot_ruw(b"<< /Type /Annot /Subtype /EigenSoort /Rect [0 0 1 1] /Contents (x) >>")
    assert bijlagen.beoordeel("v.pdf", vreemd)["reden"] == "pdf_opmerkingen"


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


# ---- review B3b op deze diff ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("naam,inhoud,reden", [
    ("word/media/image1.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><text>VERBORGEN</text></svg>', "office_beeld_vreemd"),
    ("word/media/image1.emf", b"\x01\x00\x00\x00" + b"\x00" * 40 + b" EMF" + "VERBORGEN".encode("utf-16-le"), "office_beeld_vreemd"),
    ("word/media/image1.wmf", b"\xd7\xcd\xc6\x9a" + b"\x00" * 60, "office_beeld_vreemd"),
    ("docProps/thumbnail.wmf", b"\xd7\xcd\xc6\x9a" + b"\x00" * 60, "office_beeld_vreemd"),
    ("word/media/image1.png", png_kop(800, 600), "office_foto"),
    ("word/media/image1.xml", b"<w:hdr><w:p><w:r><w:t>VERBORGEN</w:t></w:r></w:p></w:hdr>", "office_beeld_vreemd"),
])
def test_media_alleen_kleine_png_jpeg_gif(naam, inhoud, reden):
    r = bijlagen.beoordeel("n.docx", _docx({naam: inhoud}))
    assert r["door"] is False and r["reden"] == reden


def test_klein_logo_mag():
    r = bijlagen.beoordeel("n.docx", _docx({"word/media/image1.png": png_kop(200, 60, 4000)}))
    assert r["door"] is True, r["reden"]


def test_hoofddocument_via_rels_omgeleid_tekst_toch_mee():
    """_rels/.rels kan elk deel als hoofddocument aanwijzen: daarom gaat de tekst van ALLE XML-delen mee."""
    d = _docx({"_rels/.rels": '<Relationships><Relationship Id="r1" Type="x/officeDocument" Target="word/styles.xml"/></Relationships>',
               "word/styles.xml": "<w:document><w:body><w:p><w:r><w:t>GEHEIM-IN-STIJLEN</w:t></w:r></w:p></w:body></w:document>",
               "word/theme/theme1.xml": "<a:theme><a:t>GEHEIM-IN-THEMA</a:t></a:theme>"})
    r = bijlagen.beoordeel("n.docx", d)
    assert r["door"] is True and "GEHEIM-IN-STIJLEN" in r["tekst"] and "GEHEIM-IN-THEMA" in r["tekst"]


def test_attributen_met_tekst_mee():
    body = ("<w:p><w:hyperlink w:tooltip=\"TOOLTIP-verzonnen\"><w:r><w:t>link</w:t></w:r></w:hyperlink>"
            "<w:fldSimple w:instr=\" DOCPROPERTY VELDINSTR-verzonnen \"/>"
            "<v:shape><v:textpath string=\"WATERMERK-verzonnen\"/></v:shape>"
            "<wp:docPr id='3' name='x' descr='ENKELQUOTE-verzonnen'/>"
            "<w:sdt><w:dropDownList><w:listItem w:displayText=\"KEUZE-verzonnen\" w:value=\"k\"/></w:dropDownList></w:sdt></w:p>")
    d = _docx({"word/settings.xml": '<w:settings><w:docVars><w:docVar w:name="v" w:val="DOCVAR-verzonnen tekst"/></w:docVars></w:settings>'},
              extra_body=body)
    r = bijlagen.beoordeel("n.docx", d)
    assert r["door"] is True, r["reden"]
    for woord in ("TOOLTIP", "VELDINSTR", "WATERMERK", "ENKELQUOTE", "KEUZE", "DOCVAR"):
        assert woord in r["tekst"], woord
    x = _xlsx({"xl/worksheets/sheet1.xml": "<worksheet><sheetData/><dataValidations><dataValidation sqref=\"A1\" "
               "promptTitle=\"VALTITEL\" prompt=\"VALPROMPT-verzonnen\" error=\"VALFOUT-verzonnen\"/></dataValidations></worksheet>"})
    r = bijlagen.beoordeel("r.xlsx", x)
    assert r["door"] is True and all(w in r["tekst"] for w in ("VALTITEL", "VALPROMPT", "VALFOUT"))


def test_opmaakwaarden_geen_ruis():
    t = bijlagen.beoordeel("n.docx", _docx({"word/styles.xml": '<w:styles><w:latentStyles><w:lsdException w:name="heading 1"/>'
                                            '</w:latentStyles><w:style w:styleId="Kop1"><w:rFonts w:ascii="Calibri"/></w:style></w:styles>'}))["tekst"]
    assert "heading 1" not in t and "Calibri" not in t and "Kop1" not in t


def test_dubbele_zipnaam_geweigerd():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("word/document.xml", DOC.format(extra="<w:p><w:r><w:t>GEHEIM-EERSTE</w:t></w:r></w:p>"))
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            z.writestr("word/document.xml", DOC.format(extra=""))
            z.writestr("Word/Document.xml", DOC.format(extra=""))
    assert bijlagen.beoordeel("d.docx", b.getvalue())["reden"] == "office_dubbele_delen"


def test_binair_vormdeel_met_xml_wordt_gelezen():
    ct = CT.replace("</Types>", '<Default Extension="bin" ContentType="application/vnd.openxmlformats-officedocument.'
                    'wordprocessingml.printerSettings"/><Default Extension="odttf" ContentType="application/vnd.'
                    'openxmlformats-officedocument.obfuscatedFont"/></Types>')
    kop = b" " * 100 + b"<w:hdr><w:p><w:r><w:t>GEHEIM-IN-BIN</w:t></w:r></w:p></w:hdr>"
    r = bijlagen.beoordeel("n.docx", _docx({"word/printerSettings/printerSettings1.bin": kop}, ct=ct))
    assert r["door"] is True and "GEHEIM-IN-BIN" in r["tekst"]
    # een echt binair lettertype levert geen rommel op
    r = bijlagen.beoordeel("n.docx", _docx({"word/fonts/font1.odttf": bytes(range(256)) * 40}, ct=ct))
    assert r["door"] is True and "\ufffd" not in r["tekst"]


def test_binair_deel_met_ander_inhoudstype_geweigerd():
    """Via [Content_Types] als koptekst aangewezen: dan is het geen printerinstelling meer."""
    ct = CT.replace("</Types>", '<Override PartName="/word/printerSettings/printerSettings1.bin" ContentType="application/vnd.'
                    'openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>')
    r = bijlagen.beoordeel("n.docx", _docx({"word/printerSettings/printerSettings1.bin": b"<w:hdr/>"}, ct=ct))
    assert r["reden"] == "office_inhoudstype_vreemd"
    assert bijlagen.beoordeel("n.docx", _docx({"word/fonts/font1.odttf": b"\x00" * 10}))["reden"] == "office_inhoudstype_vreemd"
