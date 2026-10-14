"""
Storage for short-lived verification codes (email OTPs).

The database never holds the code itself - only a keyed hash of it. A plain
SHA-256 of a 6-digit code would fall to a million-guess brute force the moment
a database dump leaked, so the hash is an HMAC keyed with a secret derived from
settings.SECRET_KEY: without that key, a stolen row cannot be turned back into
a code. The hash also binds the code to the user and the purpose, so a hash
copied from one row (or one field) does not validate anywhere else.

The plaintext exists only in memory at the moment it is generated, long enough
to be emailed / texted. Anything that needs it later (e.g. the on-screen demo
code) must keep its own short-lived copy - see accounts.views.
"""
from django.utils.crypto import constant_time_compare, salted_hmac

_KEY_SALT = 'bookbridge.accounts.otp.v1'


def hash_otp(purpose, user_pk, otp):
    """Keyed hash of `otp` for this user and purpose, as a 64-char hex string."""
    value = f'{purpose}:{user_pk}:{(otp or "").strip()}'
    return salted_hmac(_KEY_SALT, value, algorithm='sha256').hexdigest()


def otp_matches(stored_hash, purpose, user_pk, submitted):
    if not stored_hash or not submitted:
        return False
    return constant_time_compare(stored_hash, hash_otp(purpose, user_pk, submitted))
