"""
Downloadable commission statements (PDF + CSV).

Two documents, one per direction money flows:
  - Seller statement: for marketplace sales - gross, BookBridge's commission,
    net, and whether/when/how each payout was sent.
  - Partner statement: the buyback commission a Shopkeeper Partner owes
    BookBridge, and what has been settled.

Like orders/invoice.py this uses reportlab (no OS-level dependency) and the
shared look in bookmarket/pdf_theme.py (bundled fonts, so the rupee sign
prints properly; the CSVs use plain numbers).

Periods are calendar months or Indian financial years (1 Apr - 31 Mar), cut at
midnight IST, because that is how sellers will reconcile against their own
books - the server's TIME_ZONE is UTC.
"""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal
from email.utils import parseaddr
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import Spacer

from bookmarket import pdf_theme as T

from .models import Payout
from .upi import mask_upi_id

IST = ZoneInfo('Asia/Kolkata')
MONTH_RE = re.compile(r'^(\d{4})-(0[1-9]|1[0-2])$')
FY_RE = re.compile(r'^fy-(\d{4})$')


class InvalidPeriod(ValueError):
    pass


class Period:
    def __init__(self, key, label, start, end):
        self.key, self.label, self.start, self.end = key, label, start, end  # [start, end)


def _ist(y, m, d=1):
    return datetime(y, m, d, tzinfo=IST)


def parse_period(key):
    """'2026-09' -> September 2026; 'fy-2026' -> 1 Apr 2026 - 31 Mar 2027."""
    key = (key or '').strip().lower()
    m = MONTH_RE.match(key)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        end = _ist(y + (mo == 12), 1 if mo == 12 else mo + 1)
        return Period(key, _ist(y, mo).strftime('%B %Y'), _ist(y, mo), end)
    f = FY_RE.match(key)
    if f:
        y = int(f.group(1))
        return Period(key, f'FY {y}-{str(y + 1)[-2:]}', _ist(y, 4), _ist(y + 1, 4))
    raise InvalidPeriod(key)


def period_choices(now=None):
    """What the download picker offers: this + last 11 months, then the current
    and previous financial year."""
    now = (now or timezone.now()).astimezone(IST)
    choices, y, mo = [], now.year, now.month
    for _ in range(12):
        key = f'{y}-{mo:02d}'
        choices.append((key, parse_period(key).label))
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    fy = now.year if now.month >= 4 else now.year - 1
    for start in (fy, fy - 1):
        key = f'fy-{start}'
        choices.append((key, parse_period(key).label))
    return choices


# --- Data ---------------------------------------------------------------

def seller_payouts(seller, period):
    """Cancelled payouts (refunded/voided orders) never appear: that sale
    didn't stand, so it is neither income nor commission."""
    return list(
        Payout.objects.filter(seller=seller, released_at__gte=period.start, released_at__lt=period.end)
        .exclude(status='cancelled').order_by('released_at', 'id')
    )


def seller_summary(payouts):
    z = Decimal('0.00')
    return {
        'orders': len(payouts),
        'gross': sum((p.gross_amount for p in payouts), z),
        'commission': sum((p.commission_amount for p in payouts), z),
        'net': sum((p.net_amount for p in payouts), z),
        'paid': sum((p.net_amount for p in payouts if p.status == 'paid'), z),
        'outstanding': sum((p.net_amount for p in payouts if p.status in ('pending', 'on_hold')), z),
    }


def partner_commissions(partner, period):
    return list(
        partner.commissions.select_related('buyback_request')
        .filter(created_at__gte=period.start, created_at__lt=period.end).order_by('created_at', 'id')
    )


def partner_summary(rows):
    z = Decimal('0.00')
    return {
        'buybacks': len(rows),
        'paid_to_sellers': sum((c.buyback_request.final_price or z for c in rows), z),
        'commission': sum((c.amount for c in rows), z),
        'settled': sum((c.amount for c in rows if c.is_settled), z),
        'due': sum((c.amount for c in rows if not c.is_settled), z),
    }


def statement_number(prefix, ident, period):
    return f'{prefix}-{ident}-{period.key.replace("fy-", "FY")}'


def _ist_date(dt):
    return dt.astimezone(IST).strftime('%d %b %Y') if dt else ''


# --- CSV ------------------------------------------------------------------

def csv_safe(value):
    """Spreadsheet formula-injection guard. Book titles are typed by sellers; a
    title like =HYPERLINK(...) would execute when staff or a partner opens the
    CSV in Excel. A leading ' makes Excel treat the cell as plain text."""
    value = '' if value is None else str(value)
    return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


def seller_statement_csv(seller, period, payouts):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Statement', statement_number('BBCS', seller.pk, period)])
    w.writerow(['Seller', csv_safe(seller.get_full_name() or seller.username)])
    w.writerow(['Period', period.label])
    w.writerow([])
    w.writerow([
        'Date', 'Order', 'Book', 'Sale price (INR)', 'Commission %', 'Commission (INR)', 'Net payout (INR)',
        'Payout status', 'Paid on', 'UTR',
    ])
    for p in payouts:
        w.writerow([
            _ist_date(p.released_at), p.order_ref, csv_safe(p.book_title), p.gross_amount, p.commission_percent,
            p.commission_amount, p.net_amount, p.get_status_display(), _ist_date(p.paid_at), csv_safe(p.utr),
        ])
    s = seller_summary(payouts)
    w.writerow([])
    w.writerow(['Total', '', f'{s["orders"]} orders', s['gross'], '', s['commission'], s['net']])
    w.writerow(['Paid out', '', '', '', '', '', s['paid']])
    w.writerow(['Outstanding', '', '', '', '', '', s['outstanding']])
    return buf.getvalue()


def partner_statement_csv(partner, period, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Statement', statement_number('BBPS', partner.pk, period)])
    w.writerow(['Partner', csv_safe(partner.shop_name)])
    w.writerow(['Period', period.label])
    w.writerow([])
    w.writerow(['Date', 'Book', 'Paid to seller (INR)', 'Commission (INR)', 'Status', 'Settled on'])
    for c in rows:
        w.writerow([
            _ist_date(c.created_at), csv_safe(c.buyback_request.title), c.buyback_request.final_price, c.amount,
            'Settled' if c.is_settled else 'Due', _ist_date(c.settled_at),
        ])
    s = partner_summary(rows)
    w.writerow([])
    w.writerow(['Total', f'{s["buybacks"]} buybacks', s['paid_to_sellers'], s['commission']])
    w.writerow(['Settled', '', '', s['settled']])
    w.writerow(['Due', '', '', s['due']])
    return buf.getvalue()


# --- PDF ------------------------------------------------------------------

PAGE = landscape(A4)


def _support_email():
    return parseaddr(settings.DEFAULT_FROM_EMAIL)[1] or settings.DEFAULT_FROM_EMAIL


def _meta(number, period):
    return [
        ('Statement no', number),
        ('Period', period.label),
        ('Generated', timezone.now().astimezone(IST).strftime('%d %b %Y, %I:%M %p') + ' IST'),
    ]


def _who_box(st, label, lines, width):
    return T.info_box(st, label, lines, width)


def render_seller_statement_pdf(seller, period, payouts):
    number = statement_number('BBCS', seller.pk, period)
    s = seller_summary(payouts)
    title = f'Commission statement {number}'

    def story(st, width):
        upi = mask_upi_id(getattr(getattr(seller, 'payout_profile', None), 'upi_id', ''))
        name = seller.get_full_name() or seller.username
        out = [
            T.side_by_side(
                lambda w: _who_box(st, 'Seller', [T.esc(name), f'@{T.esc(seller.username)}'], w),
                lambda w: _who_box(st, 'Payout UPI ID', [T.esc(upi) if upi else 'Not set'], w),
                width),
            Spacer(1, 6 * mm),
            T.stat_cards(st, [
                ('Orders', str(s['orders'])),
                ('Gross sales', T.money(s['gross'])),
                ('BookBridge commission', T.money(s['commission'])),
                ('Net payable to you', T.money(s['net'])),
                ('Paid out', T.money(s['paid'])),
                ('Outstanding', T.money(s['outstanding'])),
            ], width, highlight=3),
            Spacer(1, 7 * mm),
            T.section(st, 'Orders'),
        ]
        if not payouts:
            out.append(T.p('No payouts were released in this period.', st['small']))
        else:
            rows = [['Date', 'Order', 'Book', 'Sale price', '%', 'Commission', 'Net payout', 'Payout status']]
            for p in payouts:
                if p.status == 'paid':
                    status = f"{T.pill('Paid')} {_ist_date(p.paid_at)}<br/>UTR {T.esc(p.utr)}"
                elif p.status == 'on_hold':
                    status = T.pill(p.get_status_display(), 'bad')
                else:
                    status = T.pill(p.get_status_display(), 'warn')
                rows.append([
                    _ist_date(p.released_at), f'#{T.esc(p.order_ref)}', T.p(T.esc(p.book_title), st['cell']),
                    T.money(p.gross_amount), f'{p.commission_percent}', T.money(p.commission_amount),
                    T.money(p.net_amount), T.p(status, st['cell']),
                ])
            out.append(T.data_table(
                rows, [24 * mm, 22 * mm, 68 * mm, 29 * mm, 13 * mm, 29 * mm, 29 * mm, 53 * mm], right_cols=(3, 4, 5, 6)))
        out += [
            Spacer(1, 8 * mm),
            T.p(
                'Commission is BookBridge\'s platform fee on each sale, shown as at the time the payment was released. '
                'Orders refunded to the buyer are excluded. This statement is not a GST tax invoice. '
                f'Questions? Write to {T.esc(_support_email())} quoting the statement number.', st['small']),
        ]
        return out

    return T.build_document(story, title, PAGE, ('Commission Statement', _meta(number, period)))


def render_partner_statement_pdf(partner, period, rows):
    number = statement_number('BBPS', partner.pk, period)
    s = partner_summary(rows)
    title = f'Partner commission statement {number}'

    def story(st, width):
        lines = [T.esc(partner.shop_name)]
        if partner.gstin:
            lines.append(f'GSTIN: {T.esc(partner.gstin)}')
        out = [
            T.side_by_side(
                lambda w: _who_box(st, 'Partner', lines, w),
                lambda w: _who_box(st, 'Settlement', ['Deducted from your BookBridge wallet'], w),
                width),
            Spacer(1, 6 * mm),
            T.stat_cards(st, [
                ('Buybacks', str(s['buybacks'])),
                ('Paid to sellers', T.money(s['paid_to_sellers'])),
                ('Commission charged', T.money(s['commission'])),
                ('Settled', T.money(s['settled'])),
                ('Due', T.money(s['due'])),
            ], width, highlight=4),
            Spacer(1, 7 * mm),
            T.section(st, 'Buybacks'),
        ]
        if not rows:
            out.append(T.p('No buyback commission was charged in this period.', st['small']))
        else:
            data = [['Date', 'Book', 'Paid to seller', 'Commission', 'Status']]
            for c in rows:
                status = f"{T.pill('Settled')} {_ist_date(c.settled_at)}" if c.is_settled else T.pill('Due', 'warn')
                data.append([
                    _ist_date(c.created_at), T.p(T.esc(c.buyback_request.title), st['cell']),
                    T.money(c.buyback_request.final_price or 0), T.money(c.amount), T.p(status, st['cell']),
                ])
            out.append(T.data_table(data, [28 * mm, 100 * mm, 42 * mm, 42 * mm, 55 * mm], right_cols=(2, 3)))
        out += [
            Spacer(1, 8 * mm),
            T.p(
                'Commission is owed to BookBridge on books you buy directly from sellers and is settled from your '
                f'BookBridge wallet. This statement is not a GST tax invoice. Questions? {T.esc(_support_email())}.',
                st['small']),
        ]
        return out

    return T.build_document(story, title, PAGE, ('Partner Commission Statement', _meta(number, period)))
