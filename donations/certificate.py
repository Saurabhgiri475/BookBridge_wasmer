"""
Donation certificate PDF (landscape A4) built with reportlab - same pure-Python
approach as orders/invoice.py, no OS-level dependencies. The QR code comes from
reportlab itself (reportlab.graphics.barcode.qr), so nothing extra to install.

Fonts: reportlab's built-in Helvetica only covers Western European characters.
To print names in other scripts (e.g. Devanagari) set CERTIFICATE_FONT_PATH to a
.ttf that has those glyphs (e.g. Noto Sans Devanagari). Without it the donation
form only accepts names Helvetica can print - see name_is_printable().
"""
import io
import logging
import os

from django.conf import settings
from django.urls import reverse
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from bookmarket import pdf_theme as T
from bookmarket.sustainability import compute_impact

logger = logging.getLogger(__name__)

GREEN = T.GREEN
NAVY = T.BRAND
GOLD = T.ACCENT
DARK = T.INK
GREY = T.MUTED
CREAM = colors.HexColor('#fffdf7')

_custom_font_name = None


def _font():
    """(regular, bold) font names. Uses CERTIFICATE_FONT_PATH when it's set and loadable."""
    global _custom_font_name
    path = getattr(settings, 'CERTIFICATE_FONT_PATH', '')
    if path and os.path.exists(path):
        if _custom_font_name is None:
            try:
                pdfmetrics.registerFont(TTFont('BBCert', path))
                _custom_font_name = 'BBCert'
            except Exception:
                logger.exception('Could not load CERTIFICATE_FONT_PATH=%s', path)
                _custom_font_name = ''
        if _custom_font_name:
            return _custom_font_name, _custom_font_name
    reg, bold, _serif, _ = T.fonts()
    return reg, bold


def name_is_printable(text):
    """True if `text` will render correctly on the certificate."""
    path = getattr(settings, 'CERTIFICATE_FONT_PATH', '')
    if path and os.path.exists(path):
        return True
    try:
        text.encode('cp1252')  # what reportlab's standard Helvetica can print
    except UnicodeEncodeError:
        return False
    return all(ch.isprintable() for ch in text)


def verify_url(certificate):
    return settings.SITE_URL.rstrip('/') + reverse('donations:verify', args=[certificate.code])


def _fit(c, text, font, max_size, max_width, min_size=12):
    """Largest font size (<= max_size) at which `text` fits in max_width."""
    size = max_size
    while size > min_size and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _wrap(text, font, size, max_width):
    words, lines, cur = text.split(), [], ''
    for w in words:
        trial = f'{cur} {w}'.strip()
        if pdfmetrics.stringWidth(trial, font, size) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _corner(c, x, y, dx, dy, size=9 * mm):
    """Gold L-shaped corner ornament; (dx, dy) = +/-1 pointing into the page."""
    c.setStrokeColor(GOLD)
    c.setLineWidth(2.2)
    c.line(x, y, x + dx * size, y)
    c.line(x, y, x, y + dy * size)


def _seal(c, cx, cy, r):
    """Scalloped gold rosette with a book glyph-ish 'B'."""
    import math
    c.saveState()
    c.setFillColor(GOLD)
    for i in range(24):
        a = 2 * math.pi * i / 24
        c.circle(cx + math.cos(a) * r, cy + math.sin(a) * r, r * 0.22, stroke=0, fill=1)
    c.circle(cx, cy, r, stroke=0, fill=1)
    c.setFillColor(NAVY)
    c.circle(cx, cy, r * 0.78, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.circle(cx, cy, r * 0.66, stroke=1, fill=0)
    c.setFillColor(GOLD)
    c.setFont(T.fonts()[2], r * 1.05)
    c.drawCentredString(cx, cy - r * 0.34, 'B')
    c.restoreState()


def render_certificate_pdf(cert) -> bytes:
    regular, bold = _font()
    serif = T.fonts()[2]
    buf = io.BytesIO()
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(f'Book Donation Certificate {cert.code}')
    c.setAuthor(settings.SITE_NAME)
    cx = page_w / 2

    # Paper + frame
    c.setFillColor(CREAM)
    c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
    c.setStrokeColor(NAVY)
    c.setLineWidth(5)
    c.rect(9 * mm, 9 * mm, page_w - 18 * mm, page_h - 18 * mm)
    c.setLineWidth(0.8)
    c.rect(13 * mm, 13 * mm, page_w - 26 * mm, page_h - 26 * mm)
    for (x, y, dx, dy) in [(16 * mm, 16 * mm, 1, 1), (page_w - 16 * mm, 16 * mm, -1, 1),
                           (16 * mm, page_h - 16 * mm, 1, -1), (page_w - 16 * mm, page_h - 16 * mm, -1, -1)]:
        _corner(c, x, y, dx, dy)

    # Heading
    c.setFillColor(NAVY)
    c.setFont(bold, 12)
    c.drawCentredString(cx, page_h - 31 * mm, '  '.join(settings.SITE_NAME.upper()))
    c.setFillColor(DARK)
    c.setFont(serif, 34)
    c.drawCentredString(cx, page_h - 47 * mm, 'Certificate of Book Donation')
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.6)
    c.line(cx - 45 * mm, page_h - 53 * mm, cx + 45 * mm, page_h - 53 * mm)
    c.setFillColor(GOLD)
    c.circle(cx, page_h - 53 * mm, 1.6 * mm, stroke=0, fill=1)

    # Body
    c.setFillColor(GREY)
    c.setFont(regular, 12.5)
    c.drawCentredString(cx, page_h - 68 * mm, 'This certificate is proudly presented to')

    c.setFillColor(NAVY)
    name_size = _fit(c, cert.recipient_name, bold, 34, page_w - 70 * mm)
    c.setFont(bold, name_size)
    c.drawCentredString(cx, page_h - 83 * mm, cert.recipient_name)
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.line(cx - 85 * mm, page_h - 88 * mm, cx + 85 * mm, page_h - 88 * mm)

    books = f'{cert.books_count} book' + ('' if cert.books_count == 1 else 's')
    org = cert.partner_name + (f', {cert.partner_city}' if cert.partner_city else '')
    sentence = (f'for generously donating {books} to {org} through {settings.SITE_NAME} '
                f'on {cert.issued_at.strftime("%d %B %Y")}, giving them a second life and '
                f'helping more readers learn.')
    c.setFillColor(DARK)
    c.setFont(regular, 13.5)
    y = page_h - 102 * mm
    for line in _wrap(sentence, regular, 13.5, page_w - 80 * mm)[:4]:
        c.drawCentredString(cx, y, line)
        y -= 7 * mm

    # Impact strip
    impact = compute_impact(cert.books_count)
    strip_w, strip_h, strip_y = 190 * mm, 14 * mm, 49 * mm
    c.setFillColor(T.GREEN_TINT)
    c.roundRect(cx - strip_w / 2, strip_y, strip_w, strip_h, 3 * mm, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.setFont(bold, 9)
    c.drawCentredString(cx, strip_y + 8.6 * mm, 'ESTIMATED IMPACT')
    c.setFillColor(DARK)
    c.setFont(regular, 10)
    c.drawCentredString(
        cx, strip_y + 3.4 * mm,
        f'about {impact["co2_saved_kg"]:.1f} kg of CO2  |  '
        f'{impact["water_saved_litres"]:,.0f} litres of water  |  '
        f'{impact["paper_saved_kg"]:.1f} kg of paper saved',
    )

    # Footer: certificate no. (left), seal (centre), QR (right)
    c.setFillColor(DARK)
    c.setFont(bold, 10.5)
    c.drawString(28 * mm, 36 * mm, f'Certificate No: {cert.code}')
    c.setFont(regular, 9.5)
    c.setFillColor(GREY)
    c.drawString(28 * mm, 30 * mm, f'Issued: {cert.issued_at.strftime("%d %b %Y")}')
    c.drawString(28 * mm, 24.5 * mm, 'Scan the QR code or visit the link to verify.')
    url = verify_url(cert)
    c.setFont(regular, 8)
    c.drawString(28 * mm, 20 * mm, url)

    _seal(c, cx, 30 * mm, 12 * mm)

    size = 26 * mm
    qx = page_w - 28 * mm - size
    c.setFillColor(colors.white)
    c.setStrokeColor(T.LINE)
    c.roundRect(qx - 2 * mm, 18 * mm, size + 4 * mm, size + 4 * mm, 2 * mm, stroke=1, fill=1)
    widget = qr.QrCodeWidget(url)
    x0, y0, x1, y1 = widget.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, qx, 20 * mm)

    if cert.revoked:  # a revoked certificate must never look valid, even from an old download
        c.saveState()
        c.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.25))
        c.setFont(bold, 90)
        c.translate(cx, page_h / 2)
        c.rotate(25)
        c.drawCentredString(0, 0, 'REVOKED')
        c.restoreState()

    c.showPage()
    c.save()
    return buf.getvalue()
