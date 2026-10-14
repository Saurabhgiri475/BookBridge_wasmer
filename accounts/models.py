from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Avg
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.translation import gettext_lazy as _


OTP_VALIDITY_MINUTES = 10
MAX_OTP_ATTEMPTS = 5  # wrong guesses allowed before an OTP is locked out and a fresh one must be requested
OTP_RESEND_COOLDOWN_SECONDS = 60  # minimum gap between two "send me a new code" requests


def generate_referral_code():
    return get_random_string(8, allowed_chars='ABCDEFGHJKLMNPQRSTUVWXYZ23456789')  # no 0/O/1/I - easy to read aloud/type


class User(AbstractUser):
    ROLE_CHOICES = (
        ('buyer', _('Buyer')),
        ('seller', _('Seller')),
        ('both', _('Buyer & Seller')),
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='both')
    phone = models.CharField(max_length=15, blank=True)
    phone_verified = models.BooleanField(default=False)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    is_premium_seller = models.BooleanField(default=False)
    premium_until = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Referrals ---------------------------------------------------------
    # Every user gets their own shareable code (auto-generated below).
    # `referred_by` is set once, at signup, from whoever's link/code was
    # used. `referral_bonus_paid` guards against a referrer being rewarded
    # more than once for the same referred friend - see wallet/services.py.
    referral_code = models.CharField(max_length=12, unique=True, blank=True, null=True)
    referred_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals',
    )
    referral_bonus_paid = models.BooleanField(default=False)

    # --- Seller verification: Email OTP -----------------------------------
    # True from signup until the emailed code is confirmed (accounts/views.py:
    # verify_signup). While it is set the account is also is_active=False, so it
    # cannot log in - and it is the ONLY state verify_signup may re-activate,
    # so an account an admin deactivated can never be switched back on by a code.
    signup_pending = models.BooleanField(default=False)

    email_verified = models.BooleanField(default=False)
    # Keyed hash of the code (see accounts/otp.py) - never the code itself.
    email_otp = models.CharField(max_length=64, blank=True)
    email_otp_sent_at = models.DateTimeField(blank=True, null=True)
    email_otp_attempts = models.PositiveSmallIntegerField(default=0)

    # --- Fraud Detection: duplicate accounts --------------------------
    # Set by accounts.fraud_detection.find_linked_accounts() when this
    # account looks like the same real person as another account already
    # on the platform (matched on phone number - see that module).
    # Informational only - flagged
    # accounts aren't blocked, just surfaced for admin review.
    is_duplicate_flagged = models.BooleanField(default=False)
    duplicate_flag_reason = models.TextField(blank=True)

    def __str__(self):
        return self.username

    def save(self, *args, **kwargs):
        if not self.referral_code:
            code = generate_referral_code()
            while User.objects.filter(referral_code=code).exists():
                code = generate_referral_code()
            self.referral_code = code
        super().save(*args, **kwargs)

    def is_seller_verified(self):
        """A verified email is what earns the 'Verified Seller' badge."""
        return self.email_verified

    def set_otp(self, otp_field, plaintext):
        """Stores only the keyed hash of `plaintext` on `otp_field` (does not save).
        The user must already have a pk, because the hash is bound to it."""
        from .otp import hash_otp
        if not self.pk:
            raise ValueError('Save the user before issuing an OTP.')
        setattr(self, otp_field, hash_otp(otp_field, self.pk, plaintext))

    def check_email_otp(self, otp):
        return self._verify_otp('email_otp', 'email_otp_sent_at', 'email_otp_attempts', otp)

    @staticmethod
    def can_resend_otp(sent_at):
        """
        True if enough time has passed since `sent_at` to send another OTP.
        Without this, send_email_otp had no limit on how
        often a logged-in user could trigger a fresh code: each call resets
        the wrong-guess counter to 0 too, so it also doubled as a way to
        undo the brute-force lockout in _verify_otp below. A short cooldown
        closes both the SMS/email spam angle and that reset trick.
        """
        if not sent_at:
            return True
        return timezone.now() - sent_at >= timezone.timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)

    def _verify_otp(self, otp_field, sent_at_field, attempts_field, submitted_otp):
        """Used by check_email_otp. Locks out further
        guesses on a given OTP after MAX_OTP_ATTEMPTS wrong tries (the
        caller has to request a fresh code, which resets the counter) -
        without this, a 6-digit numeric OTP is guessable by brute force
        given enough unthrottled attempts."""
        stored_otp = getattr(self, otp_field)
        sent_at = getattr(self, sent_at_field)
        attempts = getattr(self, attempts_field)

        if not stored_otp or not sent_at:
            return False
        if timezone.now() - sent_at > timezone.timedelta(minutes=OTP_VALIDITY_MINUTES):
            return False
        if attempts >= MAX_OTP_ATTEMPTS:
            return False

        from .otp import otp_matches
        if otp_matches(stored_otp, otp_field, self.pk, submitted_otp):
            setattr(self, attempts_field, 0)
            self.save(update_fields=[attempts_field])
            return True

        setattr(self, attempts_field, attempts + 1)
        self.save(update_fields=[attempts_field])
        return False

    def seller_rating(self):
        from orders.models import Review
        agg = Review.objects.filter(order__book__seller=self).aggregate(avg=Avg('seller_rating'))
        return round(agg['avg'], 1) if agg['avg'] else None

    def buyer_rating(self):
        from orders.models import Review
        agg = Review.objects.filter(order__buyer=self).aggregate(avg=Avg('buyer_rating'))
        return round(agg['avg'], 1) if agg['avg'] else None


class StaffTOTPDevice(models.Model):
    """A staff member's authenticator-app secret (see accounts/twofactor.py). The secret is
    stored encrypted. `confirmed_at` stays empty until the first valid code proves the app is set up."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='totp_device')
    secret_encrypted = models.CharField(max_length=255)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(default=0, help_text='Last accepted 30-second time step; blocks replay of a used code.')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'2FA for {self.user} ({"active" if self.confirmed_at else "not confirmed"})'


class StaffRecoveryCode(models.Model):
    """One single-use recovery code (only a keyed hash is stored)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recovery_codes')
    code_hash = models.CharField(max_length=64, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
