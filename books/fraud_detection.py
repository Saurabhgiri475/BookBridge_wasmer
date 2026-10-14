"""
AI Fraud Detection.

Rule-based (no external API/ML dependency needed, works fully offline) -
mirrors price_recommendation.py and duplicate_check.py's pattern in this
app: a genuinely functional heuristic engine today, with one clean entry
point (run_fraud_scan) that a real trained model or hosted LLM could later
drop into without books.views needing to change.

Runs five checks against a new/edited listing and blends them into a
single 0-100 risk score:

  1. Fake images        - a photo that's already in use on another
                           seller's listing (reused/stock/stolen photo),
                           or a suspiciously tiny placeholder-sized image.
                           Uses a simple 8x8 average-hash fingerprint via
                           Pillow (already a dependency, see ocr_utils.py) -
                           no extra image-hashing library needed.
  2. Copied description - description text that's near-identical to
                           another seller's listing (difflib, stdlib only).
  3. Spam content        - off-platform contact info (phone/email/link),
                           spam keyword stuffing, shouty caps, repeated
                           punctuation.
  4. Suspicious price    - price far below/above the market rate for
                           comparable listings.
  5. Duplicate accounts  - the seller looks like the same person as
                           another seller already on the platform - see
                           accounts.fraud_detection.find_linked_accounts.

Listings scoring at/above FLAG_THRESHOLD are held as status='under_review'
instead of going live immediately (books.views.add_book/edit_book);
anything below that still gets the flags recorded on the listing for
admin visibility (books.admin) but publishes normally.
"""
import difflib
import re
from decimal import Decimal, InvalidOperation

from django.db.models import Avg

FLAG_THRESHOLD = 50   # score >= this -> held for manual review instead of going live
WARN_THRESHOLD = 25   # score >= this but below FLAG_THRESHOLD -> published, flagged for admin attention

SPAM_KEYWORDS = [
    'whatsapp', 'telegram', 'call me at', 'contact me at', 'click here',
    'guaranteed', 'earn money', 'work from home', 'limited time offer',
    'act now', 'no questions asked', 'cash only', 'wire transfer', 'gift card',
]
CONTACT_PATTERNS = [
    re.compile(r'\b\d{10}\b'),                            # bare 10-digit phone number
    re.compile(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b'),        # phone number with separators
    re.compile(r'\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b', re.I),   # email address
    re.compile(r'https?://\S+|www\.\S+', re.I),            # URL
]
DESCRIPTION_SIMILARITY_THRESHOLD = 0.85
DESCRIPTION_MIN_LENGTH = 40   # below this, similarity scores are too noisy to trust
IMAGE_HASH_SIZE = 8           # 8x8 average hash -> 64-bit fingerprint
IMAGE_HASH_MAX_DISTANCE = 4   # hamming distance <= this = "same photo"
IMAGE_MIN_DIMENSION = 200     # px; smaller looks like a placeholder, not a real cover photo
MAX_IMAGES_TO_COMPARE = 300   # bound the cost of comparing against other sellers' photos
MAX_DESCRIPTIONS_TO_COMPARE = 500


def _severity_weight(severity):
    return {'high': 35, 'medium': 20, 'low': 10}.get(severity, 10)


# ---------------------------------------------------------------------------
# 1. Fake / reused images
# ---------------------------------------------------------------------------
def _average_hash(image_field):
    """image_field: an ImageFieldFile (e.g. a BookImage.image). Opens it,
    computes a simple 8x8 average-hash fingerprint, and closes it again -
    doesn't assume the file was already open."""
    from PIL import Image

    image_field.open('rb')
    try:
        with Image.open(image_field) as img:
            img = img.convert('L').resize((IMAGE_HASH_SIZE, IMAGE_HASH_SIZE))
            pixels = list(img.getdata())
    finally:
        image_field.close()
    avg = sum(pixels) / len(pixels)
    bits = ''.join('1' if p > avg else '0' for p in pixels)
    return int(bits, 2)


def compute_image_hash(image_field):
    """Public, exception-swallowing wrapper around _average_hash() for
    BookImage.save() to call - returns None rather than raising if Pillow
    can't read the file, since a bad upload shouldn't break saving a
    listing."""
    from PIL import UnidentifiedImageError

    try:
        return _average_hash(image_field)
    except (UnidentifiedImageError, OSError, FileNotFoundError, ValueError):
        return None


def _hamming_distance(a, b):
    return bin(a ^ b).count('1')


def check_fake_images(images, *, seller, exclude_pk=None):
    """images: iterable of ImageFieldFile objects for the photos on this
    listing, e.g. [bi.image for bi in book.images.all()] once the listing
    has been saved. Flags a photo that's already in use on another
    seller's listing (reused/stock/stolen photo - a seller's own photos of
    their own copy won't match anyone else's), and any suspiciously tiny
    placeholder-sized image."""
    from PIL import UnidentifiedImageError

    from .models import BookImage

    images = [img for img in images if img]
    if not images:
        return []

    flags = []
    own_hashes = []
    for image_field in images:
        name = image_field.name or 'an image'
        try:
            width, height = image_field.width, image_field.height
        except (OSError, FileNotFoundError):
            continue
        if width < IMAGE_MIN_DIMENSION or height < IMAGE_MIN_DIMENSION:
            flags.append({
                'check': 'fake_images', 'severity': 'low',
                'message': f'"{name}" is very low resolution ({width}x{height}px) - '
                           f'may be a placeholder rather than a real photo of the book.',
            })
        try:
            own_hashes.append(_average_hash(image_field))
        except (UnidentifiedImageError, OSError, FileNotFoundError, ValueError):
            continue

    if own_hashes:
        # Read the stored fingerprints rather than re-decoding every other
        # seller's photos: this runs inside the request that publishes or
        # edits a listing, and opening a few hundred images with Pillow there
        # is the single slowest thing in the fraud scan. Hashes are written
        # once, at upload, by BookImage.save().
        others = BookImage.objects.exclude(book__seller=seller).exclude(image_hash=None)
        if exclude_pk:
            others = others.exclude(book_id=exclude_pk)
        others = others.values_list(
            'image_hash', 'book__seller_id', 'book__seller__username',
        ).order_by('-uploaded_at')[:MAX_IMAGES_TO_COMPARE]

        matched_sellers = {}
        for stored_hash, seller_id, seller_username in others:
            other_hash = stored_hash + 2 ** 63  # undo the signed-column shift
            for h in own_hashes:
                if _hamming_distance(h, other_hash) <= IMAGE_HASH_MAX_DISTANCE:
                    matched_sellers[seller_id] = seller_username
                    break

        if matched_sellers:
            names = ', '.join(list(matched_sellers.values())[:3])
            flags.append({
                'check': 'fake_images', 'severity': 'high',
                'message': f"One or more photos match images already used on another seller's listing "
                           f'({names}) - looks like a reused/stock photo rather than this copy.',
            })

    return flags


# ---------------------------------------------------------------------------
# 2. Copied descriptions
# ---------------------------------------------------------------------------
def check_copied_description(description, *, seller, exclude_pk=None):
    """Flags description text that's near-identical to another seller's
    listing - stdlib difflib, no extra fuzzy-matching dependency needed."""
    from .models import Book

    description = (description or '').strip()
    if len(description) < DESCRIPTION_MIN_LENGTH:
        return []

    qs = Book.objects.exclude(seller=seller).exclude(description='')
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    qs = qs.order_by('-created_at')[:MAX_DESCRIPTIONS_TO_COMPARE]

    lowered = description.lower()
    for other in qs:
        ratio = difflib.SequenceMatcher(None, lowered, other.description.lower()).ratio()
        if ratio >= DESCRIPTION_SIMILARITY_THRESHOLD:
            return [{
                'check': 'copied_description', 'severity': 'high',
                'message': f'Description is {int(ratio * 100)}% similar to seller "{other.seller.username}"\'s '
                           f'listing for "{other.title}" - looks copied rather than written for this book.',
            }]
    return []


# ---------------------------------------------------------------------------
# 3. Spam content
# ---------------------------------------------------------------------------
def check_spam_content(title, description):
    text = f"{title or ''} {description or ''}"
    if not text.strip():
        return []

    flags = []

    if any(pattern.search(text) for pattern in CONTACT_PATTERNS):
        flags.append({
            'check': 'spam', 'severity': 'medium',
            'message': 'Listing text contains a phone number, email address, or link - keep contact and '
                       'payment on-platform so both sides stay protected.',
        })

    lowered = text.lower()
    hit_keywords = [kw for kw in SPAM_KEYWORDS if kw in lowered]
    if hit_keywords:
        keyword_list = '", "'.join(hit_keywords[:3])
        flags.append({
            'check': 'spam', 'severity': 'medium',
            'message': f'Contains spam-like phrasing ("{keyword_list}").',
        })

    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 20 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        flags.append({
            'check': 'spam', 'severity': 'low',
            'message': 'Excessive capital letters - reads more like spam than a normal listing.',
        })

    if re.search(r'([!?.$₹])\1{3,}', text):
        flags.append({
            'check': 'spam', 'severity': 'low',
            'message': 'Excessive repeated punctuation.',
        })

    return flags


# ---------------------------------------------------------------------------
# 4. Suspicious price
# ---------------------------------------------------------------------------
def check_suspicious_price(price, *, title='', isbn='', category_id=None, subject='', exclude_pk=None):
    """Flags a price that's wildly out of line with comparable listings -
    either far too cheap (classic scam bait: a "deal" designed to push the
    buyer into paying off-platform) or far too expensive."""
    from .models import Book

    if price in (None, ''):
        return []
    try:
        price = Decimal(price)
    except (InvalidOperation, TypeError, ValueError):
        return []
    if price <= 0:
        # A zero/negative price is never legitimate (the form and DB reject it too) -
        # flag it as the top-severity price problem instead of waving it through.
        return [{
            'check': 'suspicious_price', 'severity': 'high',
            'message': f'Invalid price \u20b9{price}: listings must be priced at \u20b91 or more.',
        }]

    qs = Book.objects.filter(status__in=['available', 'sold', 'rented'])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    isbn = (isbn or '').strip()
    title = (title or '').strip()
    if isbn:
        similar = qs.filter(isbn=isbn)
    elif title:
        similar = qs.filter(title__iexact=title)
    elif category_id:
        similar = qs.filter(category_id=category_id)
        if subject:
            similar = similar.filter(subject__icontains=subject)
    else:
        similar = qs.none()

    if similar.count() < 3:
        return []  # not enough comparable listings to trust an average

    avg_price = similar.aggregate(a=Avg('price'))['a']
    if not avg_price:
        return []
    avg_price = Decimal(avg_price)

    if price <= avg_price * Decimal('0.25'):
        return [{
            'check': 'suspicious_price', 'severity': 'high',
            'message': f'Priced at \u20b9{price} vs a \u20b9{avg_price:.0f} average for comparable listings - '
                       f'unusually cheap can be scam bait.',
        }]
    if price <= avg_price * Decimal('0.5'):
        return [{
            'check': 'suspicious_price', 'severity': 'medium',
            'message': f'Priced well below the \u20b9{avg_price:.0f} average for comparable listings.',
        }]
    if price >= avg_price * Decimal('3'):
        return [{
            'check': 'suspicious_price', 'severity': 'medium',
            'message': f'Priced far above the \u20b9{avg_price:.0f} average for comparable listings.',
        }]
    return []


# ---------------------------------------------------------------------------
# 5. Duplicate accounts + aggregator
# ---------------------------------------------------------------------------
def run_fraud_scan(*, seller, title, description, price, isbn='', category_id=None, subject='',
                    images=None, exclude_pk=None):
    """Single entry point - runs all five checks and blends them into one
    risk score, in the same shape as price_recommendation.recommend_price().

    Returns {'score': int, 'risk_level': 'none'|'low'|'medium'|'high',
             'flags': [...], 'hold_for_review': bool}
    """
    from accounts.fraud_detection import find_linked_accounts

    flags = []
    flags += check_suspicious_price(
        price, title=title, isbn=isbn, category_id=category_id, subject=subject, exclude_pk=exclude_pk,
    )
    flags += check_copied_description(description, seller=seller, exclude_pk=exclude_pk)
    flags += check_spam_content(title, description)
    flags += check_fake_images(images or [], seller=seller, exclude_pk=exclude_pk)

    for linked in find_linked_accounts(seller):
        flags.append({
            'check': 'duplicate_account', 'severity': linked['severity'],
            'message': f"Seller may be operating a duplicate account (\"{linked['user'].username}\") - "
                       f"{linked['reason']}.",
        })

    score = min(100, sum(_severity_weight(f['severity']) for f in flags))
    if score >= FLAG_THRESHOLD:
        risk_level = 'high'
    elif score >= WARN_THRESHOLD:
        risk_level = 'medium'
    elif flags:
        risk_level = 'low'
    else:
        risk_level = 'none'

    return {
        'score': score,
        'risk_level': risk_level,
        'flags': flags,
        'hold_for_review': score >= FLAG_THRESHOLD,
    }
