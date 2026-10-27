import io
import zipfile

from meeleespod import bijlagen
from meeleespod.toetsdata import pdf_met_tekst, pdf_met_tekst_en_beeld, pdf_zonder_tekst


def _docx(tekst, foto=0):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>" + tekst + "</w:t></w:r></w:p></w:body></w:document>")
        if foto:
            z.writestr("word/media/image1.jpeg", b"\xff\xd8\xff" + b"\x01" * foto)
    return b.getvalue()


def test_oordeel_moet_expliciet():
    assert not bijlagen.mag_bijlagen(None)
    assert not bijlagen.mag_bijlagen({"klasse": "patientgebonden"})
    assert not bijlagen.mag_bijlagen({"klasse": "bekend"})
    assert not bijlagen.mag_bijlagen("zakelijk")
    assert bijlagen.mag_bijlagen({"klasse": "twijfel"})


def test_beelden_nooit():
    for b in (b"\xff\xd8\xff\xe0" + b"0" * 99, b"\x89PNG\r\n\x1a\n" + b"0" * 99, b"II*\x00" + b"0" * 99):
        assert bijlagen.beoordeel("x.pdf", b)["door"] is False


def test_pdf():
    assert bijlagen.beoordeel("a.pdf", pdf_met_tekst())["door"] is True
    r = bijlagen.beoordeel("scan.pdf", pdf_zonder_tekst())
    assert r["door"] is False and r["reden"] == "pdf_scanbeeld"
    r = bijlagen.beoordeel("klein.pdf", pdf_zonder_tekst(px=50))
    assert r["door"] is False and r["reden"] == "pdf_geen_tekstlaag"
    assert bijlagen.beoordeel("dun.pdf", pdf_met_tekst("Alleen een korte regel."))["door"] is False


def test_docx_en_andere_formaten():
    tekst = "Notulen werkoverleg. " * 20
    assert bijlagen.beoordeel("n.docx", _docx(tekst))["door"] is True
    assert bijlagen.beoordeel("n.docx", _docx(tekst, foto=300 * 1024))["reden"] == "office_foto"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("iets.txt", "x")
    assert bijlagen.beoordeel("a.zip", b.getvalue())["reden"] == "formaat_niet_toegestaan"
    assert bijlagen.beoordeel("a.exe", b"MZ" + b"0" * 100)["door"] is False
    assert bijlagen.beoordeel("a.msg", b"\xd0\xcf\x11\xe0" + b"0" * 100)["door"] is False


def test_beeld_in_tekst_pdf():
    assert bijlagen.beoordeel("brief.pdf", pdf_met_tekst_en_beeld(600, 200))["door"] is True, "briefhoofdlogo mag"
    for b, h in ((1600, 900), (900, 1200), (400, 300), (300, 300)):
        r = bijlagen.beoordeel("brief.pdf", pdf_met_tekst_en_beeld(b, h))
        assert r["door"] is False and r["reden"] == "pdf_scanbeeld", (b, h)


def test_docx_kleine_foto():
    tekst = "Notulen werkoverleg. " * 20
    assert bijlagen.beoordeel("n.docx", _docx(tekst, foto=60 * 1024))["reden"] == "office_foto"
    assert bijlagen.beoordeel("n.docx", _docx(tekst, foto=10 * 1024))["door"] is True
