"""
Shared look & feel for every PDF BookBridge generates (invoice, commission
statements, donation certificate).

- Bundled Noto fonts (bookmarket/fonts/) so the Indian rupee sign prints as a
  real rupee sign instead of "Rs." or a black box. Falls back to Helvetica if
  the font files are missing, so PDF download never breaks.
- One brand palette, one branded header band, one footer, one table style.
"""
import io
import logging
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent / 'fonts'

# Palette (matches the site: --brand / --accent in static/css/style.css)
BRAND = colors.HexColor('#1f3a5f')
BRAND_DARK = colors.HexColor('#142943')
ACCENT = colors.HexColor('#f2a71b')
INK = colors.HexColor('#1f2937')
MUTED = colors.HexColor('#6b7280')
LINE = colors.HexColor('#e5e7eb')
ZEBRA = colors.HexColor('#f8fafc')
TINT = colors.HexColor('#eef3f9')
GREEN = colors.HexColor('#1b7f4b')
GREEN_TINT = colors.HexColor('#e7f5ee')
AMBER_TINT = colors.HexColor('#fff4dc')
RED = colors.HexColor('#b42318')

_fonts = None


def fonts():
    """(regular, bold, serif_bold, rupee_symbol). Registered once."""
    global _fonts
    if _fonts is None:
        try:
            pdfmetrics.registerFont(TTFont('BB-Sans', str(FONT_DIR / 'NotoSans-Regular.ttf')))
            pdfmetrics.registerFont(TTFont('BB-Sans-Bold', str(FONT_DIR / 'NotoSans-Bold.ttf')))
            pdfmetrics.registerFont(TTFont('BB-Serif-Bold', str(FONT_DIR / 'NotoSerif-Bold.ttf')))
            pdfmetrics.registerFontFamily('BB-Sans', normal='BB-Sans', bold='BB-Sans-Bold',
                                          italic='BB-Sans', boldItalic='BB-Sans-Bold')
            _fonts = ('BB-Sans', 'BB-Sans-Bold', 'BB-Serif-Bold', '\u20b9')
        except Exception:
            logger.exception('PDF fonts could not be loaded; falling back to Helvetica')
            _fonts = ('Helvetica', 'Helvetica-Bold', 'Times-Bold', 'Rs. ')
    return _fonts


def money(amount):
    """Rupee amount, e.g. ₹1,250.00 (Rs. 1,250.00 if the fonts are unavailable)."""
    sym = fonts()[3]
    return f'{sym}{Decimal(amount):,.2f}'


def styles():
    reg, bold, _serif, _ = fonts()
    base = dict(fontName=reg, textColor=INK)
    return {
        'normal': ParagraphStyle('bb-normal', fontSize=9.5, leading=13.5, **base),
        'cell': ParagraphStyle('bb-cell', fontSize=8.5, leading=11.5, **base),
        'cell_r': ParagraphStyle('bb-cell-r', fontSize=8.5, leading=11.5, alignment=2, **base),
        'small': ParagraphStyle('bb-small', fontName=reg, fontSize=8, leading=11.5, textColor=MUTED),
        'label': ParagraphStyle('bb-label', fontName=bold, fontSize=7.5, leading=10, textColor=MUTED),
        'h2': ParagraphStyle('bb-h2', fontName=bold, fontSize=11.5, leading=15, textColor=BRAND,
                             spaceBefore=2, spaceAfter=5),
        'party_name': ParagraphStyle('bb-party', fontName=bold, fontSize=10.5, leading=14, textColor=INK),
    }


# --- Page chrome -----------------------------------------------------------

def _page_chrome(doc_title, page_size):
    """Returns a canvas callback: brand band on top, thin accent line, footer."""
    reg, bold, _, _ = fonts()
    page_w, page_h = page_size

    def draw(canvas, doc):
        canvas.saveState()
        # top band
        band_h = 26 * mm
        canvas.setFillColor(BRAND)
        canvas.rect(0, page_h - band_h, page_w, band_h, stroke=0, fill=1)
        canvas.setFillColor(ACCENT)
        canvas.rect(0, page_h - band_h - 1.6 * mm, page_w, 1.6 * mm, stroke=0, fill=1)
        # logo mark: rounded square with "B"
        x, y, s = 15 * mm, page_h - band_h + 5 * mm, 16 * mm
        canvas.setFillColor(ACCENT)
        canvas.roundRect(x, y, s, s, 3 * mm, stroke=0, fill=1)
        canvas.setFillColor(BRAND_DARK)
        canvas.setFont(bold, 18)
        canvas.drawCentredString(x + s / 2, y + 4.6 * mm, 'B')
        canvas.setFillColor(colors.white)
        canvas.setFont(bold, 17)
        canvas.drawString(x + s + 4 * mm, y + 8 * mm, settings.SITE_NAME)
        canvas.setFont(reg, 8)
        canvas.setFillColor(colors.HexColor('#cbd5e1'))
        canvas.drawString(x + s + 4 * mm, y + 3 * mm, 'Give books a second story')
        # footer
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(15 * mm, 15 * mm, page_w - 15 * mm, 15 * mm)
        canvas.setFont(reg, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 10 * mm, f'{settings.SITE_NAME}  |  {doc_title}')
        canvas.drawRightString(page_w - 15 * mm, 10 * mm, f'Page {doc.page}')
        canvas.restoreState()

    return draw


def build_document(story_fn, doc_title, page_size, doc_label_right=None):
    """Builds a PDF with the standard chrome. `story_fn(styles, content_width)`
    returns the platypus story. `doc_label_right` (title, [(k, v), ...]) is drawn
    inside the header band's right side."""
    buf = io.BytesIO()
    page_w, page_h = page_size
    left = right = 15 * mm
    top = 26 * mm + 1.6 * mm + 7 * mm
    bottom = 20 * mm
    doc = BaseDocTemplate(
        buf, pagesize=page_size, leftMargin=left, rightMargin=right, topMargin=top, bottomMargin=bottom,
        title=doc_title, author=settings.SITE_NAME,
    )
    frame = Frame(left, bottom, page_w - left - right, page_h - top - bottom, id='f',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    chrome = _page_chrome(doc_title, page_size)

    def on_page(canvas, d):
        chrome(canvas, d)
        if doc_label_right:
            _draw_band_meta(canvas, page_size, *doc_label_right)

    doc.addPageTemplates([PageTemplate(id='main', frames=[frame], onPage=on_page)])
    doc.build(story_fn(styles(), page_w - left - right))
    return buf.getvalue()


def _draw_band_meta(canvas, page_size, title, pairs):
    reg, bold, _, _ = fonts()
    page_w, page_h = page_size
    canvas.saveState()
    r = page_w - 15 * mm
    canvas.setFillColor(colors.white)
    canvas.setFont(bold, 13)
    canvas.drawRightString(r, page_h - 11 * mm, title.upper())
    canvas.setFont(reg, 8.5)
    canvas.setFillColor(colors.HexColor('#dbe4ef'))
    y = page_h - 16.2 * mm
    for k, v in pairs:
        canvas.drawRightString(r, y, f'{k}: {v}')
        y -= 3.8 * mm
    canvas.restoreState()


# --- Building blocks -------------------------------------------------------

def p(text, style):
    return Paragraph(text, style)


def esc(text):
    return escape(str(text or ''))


def section(st, title):
    return Paragraph(esc(title), st['h2'])


def stat_cards(st, items, width, highlight=None):
    """Row of summary cards. items: [(label, value_str)]. `highlight` = index
    of the card to emphasise (net payable, total due...)."""
    reg, bold, _, _ = fonts()
    n = len(items)
    gap = 3 * mm
    col_w = (width - gap * (n - 1)) / n
    cells, widths, style = [], [], []
    for i, (label, value) in enumerate(items):
        if i:
            cells.append('')
            widths.append(gap)
        hl = (i == highlight)
        lab = ParagraphStyle('sl', fontName=bold, fontSize=7, leading=9.5,
                             textColor=colors.HexColor('#dbe4ef') if hl else MUTED)
        val = ParagraphStyle('sv', fontName=bold, fontSize=12.5 if n <= 5 else 11, leading=16,
                             textColor=colors.white if hl else INK)
        cells.append([Paragraph(esc(label).upper(), lab), Paragraph(esc(value), val)])
        widths.append(col_w)
        c = len(cells) - 1
        style += [
            ('BACKGROUND', (c, 0), (c, 0), BRAND if hl else TINT),
            ('LINEABOVE', (c, 0), (c, 0), 2, ACCENT if hl else BRAND),
        ]
    t = Table([cells], colWidths=widths, hAlign='LEFT')
    t.setStyle(TableStyle(style + [
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    return t


def data_table(rows, widths, right_cols=(), total_row=False):
    """Zebra-striped table with a brand header row. rows[0] is the header."""
    reg, bold, _, _ = fonts()
    t = Table(rows, colWidths=widths, repeatRows=1, hAlign='LEFT')
    cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), BRAND),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), bold),
        ('FONTNAME', (0, 1), (-1, -1), reg),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('FONTSIZE', (0, 1), (-1, -1), 8.5),
        ('TEXTCOLOR', (0, 1), (-1, -1), INK),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, LINE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, ZEBRA]),
    ] + [('ALIGN', (c, 0), (c, -1), 'RIGHT') for c in right_cols]
    if total_row:
        cmds += [
            ('BACKGROUND', (0, -1), (-1, -1), TINT),
            ('FONTNAME', (0, -1), (-1, -1), bold),
            ('LINEABOVE', (0, -1), (-1, -1), 1.2, BRAND),
        ]
    t.setStyle(TableStyle(cmds))
    return t


def info_box(st, label, lines, width):
    """Titled panel for Buyer / Seller / Partner details."""
    inner = [Paragraph(esc(label).upper(), st['label']), Spacer(1, 2)]
    inner += [Paragraph(l, st['party_name'] if i == 0 else st['normal']) for i, l in enumerate(lines) if l]
    t = Table([[inner]], colWidths=[width])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.white),
        ('BOX', (0, 0), (-1, -1), 0.6, LINE),
        ('LINEBEFORE', (0, 0), (0, -1), 3, BRAND),
        ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
    ]))
    return t


def side_by_side(left, right, width, gap=6 * mm):
    w = (width - gap) / 2
    t = Table([[left(w), '', right(w)]], colWidths=[w, gap, w], hAlign='LEFT')
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    return t


def pill(text, kind='ok'):
    """Small coloured status text for use inside a Paragraph."""
    col = {'ok': '#1b7f4b', 'warn': '#b7791f', 'bad': '#b42318'}[kind]
    return f'<font color="{col}"><b>{esc(text)}</b></font>'
