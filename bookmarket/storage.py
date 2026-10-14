"""
Private file storage for sensitive uploads (partner ID / shop proofs, paid
digital files).

Files saved here live under settings.PRIVATE_MEDIA_ROOT, which is never
exposed by a URL: `url()` deliberately raises, and MEDIA_URL does not map to
this folder. The only way to read one is through a Django view that checks
who is asking (partners.views.proof_file, orders.views.download_digital_file).

Cloud storage: when S3_PRIVATE_BUCKET is set (see settings.py / DEPLOY_WASMER.md)
get_private_storage() returns PrivateS3Storage, a private S3-compatible bucket
(Cloudflare R2, AWS S3, Backblaze B2, ...). Files are still only ever read
through the same authorised views, which call field.open().
"""
from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateFileSystemStorage(FileSystemStorage):
    def __init__(self, **kwargs):
        # Owner-only permissions on disk (POSIX); ignored on Windows.
        kwargs.setdefault('file_permissions_mode', 0o600)
        kwargs.setdefault('directory_permissions_mode', 0o700)
        super().__init__(**kwargs)

    # Read the location from settings on every use (not cached) so that
    # override_settings(PRIVATE_MEDIA_ROOT=...) works in tests.
    @property
    def base_location(self):
        return str(settings.PRIVATE_MEDIA_ROOT)

    @property
    def location(self):
        import os
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError(
            'Private files have no public URL. Serve them through an authorised view '
            '(see bookmarket/storage.py).'
        )


def get_private_storage():
    """Callable storage for model fields (keeps migrations free of paths)."""
    if getattr(settings, 'USE_S3_PRIVATE', False):
        from storages.backends.s3 import S3Storage

        class PrivateS3Storage(S3Storage):
            def url(self, name, *args, **kwargs):
                raise ValueError(
                    'Private files have no public URL. Serve them through an authorised view '
                    '(see bookmarket/storage.py).'
                )

        return PrivateS3Storage(**settings.S3_PRIVATE_OPTIONS)
    return PrivateFileSystemStorage()
