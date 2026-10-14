"""
Shared helper for serving a private image through an authorised view.

Callers do their own permission check FIRST (and raise Http404 for anyone who
may not see the file, so the URL never confirms a file exists), then hand the
FieldFile to `private_image_response()`.
"""
import mimetypes
import os

from django.http import FileResponse, Http404

_INLINE_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


def private_image_response(field):
    if not field:
        raise Http404
    try:
        handle = field.open('rb')
    except (FileNotFoundError, ValueError):
        raise Http404
    content_type = mimetypes.guess_type(field.name)[0] or 'application/octet-stream'
    inline = content_type in _INLINE_IMAGE_TYPES
    response = FileResponse(
        handle, content_type=content_type if inline else 'application/octet-stream',
        as_attachment=not inline, filename=os.path.basename(field.name),
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response
