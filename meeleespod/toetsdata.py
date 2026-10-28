"""Verzonnen toetsdata (de repo is privé, maar de regel blijft: nooit echte gegevens). Kleine pdf's met de hand gebouwd."""
import zlib

TEKST = ("Offerte nummer 2026-0042 voor het onderhoud van de koffiemachine in de wachtkamer. "
         "Prijs per jaar 480 euro exclusief btw, looptijd twaalf maanden, opzegtermijn een maand. "
         "Leverancier Voorbeeld Koffie BV, Dorpsstraat 1, 1234 AB Verzonnen.")


def _pdf(objecten):
    uit = bytearray(b"%PDF-1.4\n")
    plekken = []
    for i, o in enumerate(objecten, 1):
        plekken.append(len(uit))
        uit += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(uit)
    uit += f"xref\n0 {len(objecten) + 1}\n0000000000 65535 f \n".encode()
    for p in plekken:
        uit += f"{p:010d} 00000 n \n".encode()
    uit += f"trailer\n<< /Size {len(objecten) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(uit)


def _stroom(data, extra=b""):
    return b"<< /Length " + str(len(data)).encode() + extra + b" >>\nstream\n" + data + b"\nendstream"


def pdf_met_tekst(tekst=TEKST):
    regels = [tekst[i:i + 80] for i in range(0, len(tekst), 80)]
    inhoud = b"BT /F1 11 Tf 50 780 Td 14 TL " + b" ".join(
        b"(" + r.replace("(", "").replace(")", "").encode("latin-1") + b") '" for r in regels) + b" ET"
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        _stroom(inhoud),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def pdf_zonder_tekst(px=1200):
    """Een 'scan': één grijs beeld van px x px over de hele pagina, geen tekstlaag."""
    beeld = zlib.compress(bytes([200]) * (px * px))
    inhoud = b"q 595 0 0 842 0 0 cm /Im1 Do Q"
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
        _stroom(inhoud),
        _stroom(beeld, f" /Type /XObject /Subtype /Image /Width {px} /Height {px} /ColorSpace /DeviceGray "
                       f"/BitsPerComponent 8 /Filter /FlateDecode".encode()),
    ])


def pdf_met_tekst_en_beeld(b, h, tekst=TEKST):
    """Tekstlaag plus één beeld van b x h px (logo of foto)."""
    regels = [tekst[i:i + 80] for i in range(0, len(tekst), 80)]
    inhoud = b"q 100 0 0 50 50 50 cm /Im1 Do Q BT /F1 11 Tf 50 780 Td 14 TL " + b" ".join(
        b"(" + r.replace("(", "").replace(")", "").encode("latin-1") + b") '" for r in regels) + b" ET"
    beeld = zlib.compress(bytes([90]) * (b * h))
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> /Contents 4 0 R >>",
        _stroom(inhoud),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stroom(beeld, f" /Type /XObject /Subtype /Image /Width {b} /Height {h} /ColorSpace /DeviceGray "
                       f"/BitsPerComponent 8 /Filter /FlateDecode".encode()),
    ])


def _tekstpagina(extra=b""):
    regels = [TEKST[i:i + 80] for i in range(0, len(TEKST), 80)]
    inhoud = b"BT /F1 11 Tf 50 780 Td 14 TL " + b" ".join(
        b"(" + r.replace("(", "").replace(")", "").encode("latin-1") + b") '" for r in regels) + b" ET"
    return inhoud


def pdf_met_annotatie(soort=b"Text", formulier=False):
    """Tekstpagina plus één annotatie (/Text = notitie, /Link = koppeling, /Widget = formulierveld). Verzonnen inhoud."""
    if soort == b"Widget":
        annot = b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (veld) /V (Verzonnen waarde) /Rect [50 600 250 620] /P 3 0 R >>"
    elif soort == b"Link":
        annot = b"<< /Type /Annot /Subtype /Link /Rect [50 600 250 620] /A << /S /URI /URI (https://voorbeeld.nl) >> >>"
    else:
        annot = b"<< /Type /Annot /Subtype /" + soort + b" /Rect [50 500 70 520] /Contents (Verzonnen notitie) /P 3 0 R >>"
    catalogus = b"<< /Type /Catalog /Pages 2 0 R" + (b" /AcroForm << /Fields [6 0 R] >>" if formulier else b"") + b" >>"
    return _pdf([
        catalogus,
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R /Annots [6 0 R] >>",
        _stroom(_tekstpagina()),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        annot,
    ])


def pdf_met_objectstroom(binnen, filter_=b"/FlateDecode", parms=b""):
    """Een pdf-fragment met een (gecomprimeerde) objectstroom; alleen voor de structuurtoets, niet om te openen."""
    data = zlib.compress(binnen) if filter_ == b"/FlateDecode" else binnen
    return (b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog >>\nendobj\n7 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter "
            + filter_ + parms + b" /Length " + str(len(data)).encode() + b" >>\nstream\n" + data
            + b"\nendstream\nendobj\n%%EOF\n")
