"""
Validators shared by the signup form and the partner application form.

* GSTIN  - structure + state code + embedded PAN + the mod-36 check digit.
* PAN    - structure + the 4th-character holder-type code.
* Proof images - size / real-image check, HEIC/HEIF (iPhone) support, EXIF
  rotation applied, metadata (incl. GPS) stripped, re-encoded as JPEG/PNG.

None of this *verifies* a number with the government (that needs the GST
portal / NSDL / DigiLocker APIs). It rejects typos and obviously fake values
before a human reviewer has to look at them.
"""
import io
import os
import re

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile, UploadedFile
from django.utils.translation import gettext_lazy as _
from PIL import Image, ImageOps, UnidentifiedImageError

try:  # HEIC/HEIF support is optional - without it iPhone photos are rejected with a clear message.
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # pragma: no cover - depends on the environment
    HEIC_SUPPORTED = False


# --- GSTIN -------------------------------------------------------------------

_GST_CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_GSTIN_RE = re.compile(r'^(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])$')
# 01-38 are state/UT codes (incl. the 2025 ones), 97 = Other Territory, 99 = Centre jurisdiction.
_VALID_STATE_CODES = {f'{n:02d}' for n in range(1, 39)} | {'97', '99'}


def gstin_check_char(first_14):
    """The GSTIN check digit: Luhn-style mod-36 over the first 14 characters."""
    total = 0
    for index, char in enumerate(first_14):
        value = _GST_CHARS.index(char) * (1 if index % 2 == 0 else 2)
        total += value // 36 + value % 36
    return _GST_CHARS[(36 - total % 36) % 36]


def normalize_gstin(value):
    return re.sub(r'[\s-]', '', (value or '')).upper()


def validate_gstin(value):
    """Raises ValidationError unless `value` is a structurally valid GSTIN."""
    gstin = normalize_gstin(value)
    if len(gstin) != 15:
        raise ValidationError('A GSTIN is exactly 15 characters, e.g. 22AAAAA0000A1ZC.', code='gstin_length')
    match = _GSTIN_RE.match(gstin)
    if not match:
        raise ValidationError(
            'That GSTIN is not in the right format (2 digits, 5 letters, 4 digits, 1 letter, 1 character, "Z", 1 character).',
            code='gstin_format',
        )
    if match.group(1) not in _VALID_STATE_CODES:
        raise ValidationError('The first two digits of a GSTIN are a state code (01-38) - please re-check it.', code='gstin_state')
    if gstin_check_char(gstin[:14]) != gstin[14]:
        raise ValidationError('This GSTIN fails its checksum - there is probably a typo. Please re-check it.', code='gstin_checksum')
    return gstin


# --- PAN ---------------------------------------------------------------------

_PAN_RE = re.compile(r'^[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]$')


def normalize_pan(value):
    return re.sub(r'\s', '', (value or '')).upper()


def validate_pan(value):
    pan = normalize_pan(value)
    if not _PAN_RE.match(pan):
        raise ValidationError('Enter a valid 10-character PAN, e.g. ABCPE1234F.', code='pan_format')
    return pan


# --- Proof images ------------------------------------------------------------

MAX_PROOF_BYTES = 5 * 1024 * 1024
MAX_PROOF_PIXELS = 40_000_000  # decompression-bomb guard; a 12 MP phone photo is ~12M
ALLOWED_PROOF_FORMATS = {'JPEG', 'PNG', 'WEBP', 'HEIF', 'MPO'}  # MPO = some phones' JPEG variant
PROOF_ACCEPT_ATTR = 'image/jpeg,image/png,image/webp,image/heic,image/heif,.jpg,.jpeg,.png,.webp,.heic,.heif'
PROOF_HELP_TEXT = _('JPG, PNG, WebP or HEIC (iPhone photos are fine) - up to 5 MB. Make sure all four corners and the text are readable.')


def clean_proof_image(upload, label='image'):
    """Validate an uploaded proof photo and return a cleaned UploadedFile.

    Anything that is not an UploadedFile (e.g. the already-saved FieldFile when
    someone edits an application and leaves the photo alone) is returned as is.
    HEIC/HEIF is converted to JPEG and everything is re-encoded, which drops
    EXIF/GPS metadata and any payload hidden in the original bytes."""
    if not isinstance(upload, UploadedFile):
        return upload

    if upload.size > MAX_PROOF_BYTES:
        raise ValidationError(f'The {label} is {upload.size / 1048576:.1f} MB - the limit is 5 MB. Try a lower camera resolution or a screenshot.')

    ext = os.path.splitext(upload.name)[1].lower()
    if ext in ('.heic', '.heif') and not HEIC_SUPPORTED:
        raise ValidationError('HEIC photos are not supported on this server yet - please upload a JPG or PNG (iPhone: Settings > Camera > Formats > Most Compatible).')

    try:
        upload.seek(0)
        image = Image.open(upload)
        image.verify()          # cheap structural check
        upload.seek(0)
        image = Image.open(upload)  # verify() invalidates the handle - reopen to decode
        fmt = (image.format or '').upper()
        if image.width * image.height > MAX_PROOF_PIXELS:
            raise ValidationError(f'The {label} resolution is too large - please upload a smaller photo.')
        if fmt not in ALLOWED_PROOF_FORMATS:
            raise ValidationError(f'The {label} must be a JPG, PNG, WebP or HEIC photo (got {fmt.title() or "an unknown format"}).')
        image = ImageOps.exif_transpose(image)
        image.load()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValidationError(f'We could not read that {label}. Upload a JPG, PNG, WebP or HEIC photo, not a PDF or a renamed file.')

    keep_png = fmt == 'PNG' and image.mode in ('RGBA', 'LA', 'P')
    buf = io.BytesIO()
    if keep_png:
        image.save(buf, 'PNG', optimize=True)
        new_ext, content_type = '.png', 'image/png'
    else:
        image.convert('RGB').save(buf, 'JPEG', quality=88, optimize=True)
        new_ext, content_type = '.jpg', 'image/jpeg'

    base = os.path.splitext(os.path.basename(upload.name))[0][:60] or 'upload'
    size = buf.tell()
    buf.seek(0)
    return InMemoryUploadedFile(buf, getattr(upload, 'field_name', None), base + new_ext, content_type, size, None)
