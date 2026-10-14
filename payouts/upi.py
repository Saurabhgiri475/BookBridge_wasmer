"""
UPI ID (VPA) helpers: validation, masking, and the `upi://pay` deep link
staff can tap to open their UPI app with the payee/amount pre-filled.

This only checks that a VPA is well-formed (`handle@bank`). It cannot tell
whether the VPA actually exists or belongs to the seller - that is only
knowable by the UPI app's own name lookup when staff send the money, which is
why staff are asked to compare the name it shows against the seller's name.
"""
import re
from urllib.parse import quote

UPI_ID_RE = re.compile(r'^[a-z0-9][a-z0-9.\-_]{1,255}@[a-z][a-z0-9]{1,63}$')


def normalize_upi_id(value):
    return (value or '').strip().lower()


def is_valid_upi_id(value):
    return bool(UPI_ID_RE.match(normalize_upi_id(value)))


def mask_upi_id(value):
    """'rahul.sharma@okhdfcbank' -> 'ra****@okhdfcbank' - for places that show
    the ID to someone who doesn't need the full thing (emails, statements)."""
    value = normalize_upi_id(value)
    if '@' not in value:
        return ''
    handle, bank = value.split('@', 1)
    return f'{handle[:2]}{"*" * max(len(handle) - 2, 2)}@{bank}'


def upi_deeplink(upi_id, payee_name, amount, note):
    """Standard UPI intent URL (NPCI linking spec). `quote` keeps names and
    notes with spaces/&/# from breaking out of their own query parameter."""
    return (
        f'upi://pay?pa={quote(normalize_upi_id(upi_id), safe="@")}'
        f'&pn={quote(payee_name or "", safe="")}'
        f'&am={quote(str(amount), safe=".")}'
        f'&cu=INR&tn={quote(note or "", safe="")}'
    )
