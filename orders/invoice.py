"""
Order invoice/receipt PDF generation.

Uses reportlab (pure Python, no OS-level dependency like wkhtmltopdf or
Cairo/Pango) so this works the same way on Windows/macOS/Linux with just
`pip install -r requirements.txt`, matching the rest of this project's
"it just works" philosophy.

This is a receipt/bill of sale, not a GST tax invoice: BookBridge connects
individual buyers and sellers trading used books peer-to-peer, and neither
side is assumed to be a GST-registered business, so no GST/HSN fields are
shown. If BookBridge itself ever needs to invoice a *seller* for its own
platform commission, that's a different, separate document.
"""
from email.utils import parseaddr

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Spacer, Table, TableStyle

from bookmarket import pdf_theme as T


def _lines(*parts):
    return [T.esc(x) for x in parts if x]


def render_invoice_pdf(order) -> bytes:
    """Builds the invoice PDF for `order` and returns it as raw bytes."""
    buyer = order.buyer
    seller = order.book.seller
    ref = str(order.order_id)[:8]
    support = parseaddr(settings.DEFAULT_FROM_EMAIL)[1] or settings.DEFAULT_FROM_EMAIL
    reg, bold, _serif, _ = T.fonts()

    def story(st, width):
        out = [
            T.side_by_side(
                lambda w: T.info_box(st, 'Sold to (Buyer)', _lines(
                    buyer.get_full_name() or buyer.username, buyer.email, buyer.phone,
                    order.delivery_address, f'{order.delivery_city} {order.delivery_pincode}'.strip()), w),
                lambda w: T.info_box(st, 'Sold by (Seller)', _lines(
                    seller.get_full_name() or seller.username, seller.email, seller.phone, seller.city), w),
                width),
            Spacer(1, 8 * mm),
            T.section(st, 'Order details'),
        ]

        item_label = order.book.title
        if order.is_rental:
            item_label += f' (Rental - {order.rental_days} days)'
        rows = [['Description', 'Qty', 'Amount']]
        rows.append([T.p(T.esc(item_label), st['cell']), '1', T.money(order.final_price)])
        if not order.is_digital:
            rows.append(['Delivery charge', '1', T.money(order.delivery_charge)])
        if order.coupon_discount:
            code = order.coupon.code if order.coupon else 'applied'
            rows.append([f'Coupon discount ({T.esc(code)})', '', '-' + T.money(order.coupon_discount)])
        rows.append(['Total paid', '', T.money(order.total_amount)])
        out.append(T.data_table(rows, [width - 22 * mm - 40 * mm, 22 * mm, 40 * mm],
                                right_cols=(1, 2), total_row=True))
        out.append(Spacer(1, 8 * mm))

        payment = getattr(order, 'payment', None)
        pay = [('Payment method', order.get_payment_method_display())]
        if payment:
            pay.append(('Payment status', payment.get_status_display()))
            if payment.transaction_ref:
                pay.append(('Transaction ref', payment.transaction_ref))
        cells = [[T.p(T.esc(k).upper(), st['label']), T.p(T.esc(v), st['normal'])] for k, v in pay]
        t = Table(cells, colWidths=[40 * mm, width - 40 * mm], hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), T.TINT),
            ('LINEABOVE', (0, 0), (-1, 0), 2, T.GREEN),
            ('LEFTPADDING', (0, 0), (-1, -1), 10), ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        out += [T.section(st, 'Payment'), t, Spacer(1, 12 * mm)]
        out.append(T.p(
            'This is a receipt for a peer-to-peer sale of a used book facilitated by '
            f'{T.esc(settings.SITE_NAME)}. Not a GST tax invoice.<br/>'
            f'Questions about this order? Contact the seller through {T.esc(settings.SITE_NAME)} chat, '
            f'or reach us at {T.esc(support)}.', st['small']))
        return out

    meta = [('Order', f'#{ref}'), ('Date', order.created_at.strftime('%d %b %Y, %I:%M %p'))]
    return T.build_document(story, f'Invoice {ref}', A4, ('Invoice / Receipt', meta))
