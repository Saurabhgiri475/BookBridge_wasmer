"""
Shared image preparation for the hosted OCR engines (OCR.space, Gemini).

Phone photos are big (often 3-8 MB) and carry an EXIF rotation flag. OCR.space's
free tier rejects files over 1 MB, so before any upload we fix the
orientation, shrink the longest side, and re-encode as JPEG until it fits.
"""
import io

from PIL import Image, ImageOps


class OcrEngineError(Exception):
    """Base class for any hosted-OCR failure. scan_cover_image() catches this
    and moves on to the next engine in the chain."""


# Try the biggest/best-looking version first, then step down until it fits.
_SIDES = (1800, 1400, 1000, 700)
_QUALITIES = (85, 70, 55)


def prepare_image(image_bytes: bytes, max_bytes: int = 900 * 1024) -> tuple[bytes, str]:
    """Returns (jpeg_bytes, mime_type) no larger than max_bytes."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGB':
            img = img.convert('RGB')
    except Exception as exc:
        raise OcrEngineError(f'Could not read the image: {exc}') from exc

    data = b''
    for side in _SIDES:
        candidate = img.copy()
        candidate.thumbnail((side, side), Image.LANCZOS)
        for quality in _QUALITIES:
            buf = io.BytesIO()
            candidate.save(buf, 'JPEG', quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data, 'image/jpeg'
    raise OcrEngineError('Image is too large to send even after compression.')
