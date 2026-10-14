"""
SMTP backend that trusts the CA bundle shipped with `certifi`.

Why this exists: on Wasmer Edge, Python runs as WebAssembly (WASI) and the
runtime has no system CA certificate store. Django's stock SMTP backend uses
ssl.create_default_context(), which loads the *system* CAs, so STARTTLS to
Gmail / SendGrid / etc. fails with

    ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED:
    unable to get local issuer certificate

Pointing the TLS context at certifi's bundle (already installed, because
`requests` depends on it) fixes that while keeping full certificate
verification ON. Elsewhere it behaves exactly like Django's own backend.

Enable with:  EMAIL_BACKEND=bookmarket.email_backend.CertifiSMTPEmailBackend
"""
import ssl

from django.core.mail.backends.smtp import EmailBackend as DjangoSMTPEmailBackend
from django.utils.functional import cached_property


class CertifiSMTPEmailBackend(DjangoSMTPEmailBackend):
    @cached_property
    def ssl_context(self):
        # A client cert/key was explicitly configured - keep Django's behaviour.
        if self.ssl_certfile or self.ssl_keyfile:
            return super().ssl_context
        try:
            import certifi
        except ImportError:  # pragma: no cover - requests always pulls it in
            return ssl.create_default_context()
        return ssl.create_default_context(cafile=certifi.where())
