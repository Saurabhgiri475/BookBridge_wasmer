"""
S3-compatible storage for PUBLIC-facing media (book photos, avatars) when the bucket itself is kept private
(S3_PUBLIC_BUCKET set but S3_PUBLIC_DOMAIN left empty - e.g. Backblaze B2 without a public bucket).

Every URL is a signed, expiring link. A fresh signature on every page view would give every image a new address
each time, so browsers could never cache them; this class re-uses a signed URL for most of its lifetime instead.
Imported only when that mode is configured (it needs django-storages).
"""
import time

from storages.backends.s3 import S3Storage

_URL_LIFETIME = 604800       # signed links stay valid 7 days (the maximum S3 signature v4 allows)
_REUSE_FOR = 6 * 86400       # ...and the same link is handed out for the first 6 of them
_MAX_ENTRIES = 5000
_cache = {}                  # name -> (url, reuse_until); per worker process, safe to lose at any time


class SignedMediaS3Storage(S3Storage):
    def url(self, name, parameters=None, expire=None, http_method=None):
        if parameters or expire or http_method:
            return super().url(name, parameters=parameters, expire=expire, http_method=http_method)
        now = time.time()
        hit = _cache.get(name)
        if hit and hit[1] > now:
            return hit[0]
        url = super().url(name, expire=_URL_LIFETIME)
        if len(_cache) >= _MAX_ENTRIES:
            _cache.clear()
        _cache[name] = (url, now + _REUSE_FOR)
        return url
