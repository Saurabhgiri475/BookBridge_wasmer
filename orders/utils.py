import secrets
import string


def generate_otp(length: int = 6) -> str:
    """Delivery-confirmation / verification OTP - our own trust checkpoint
    for releasing escrowed payment (and for email verification),
    independent of whatever the courier reports. Uses `secrets` (not
    `random`) since these gate real money and identity actions, and
    defaults to 6 digits rather than 4 to make blind guessing harder -
    paired with the attempt-lockout in accounts.models._verify_otp and
    orders.views.confirm_delivery."""
    return ''.join(secrets.choice(string.digits) for _ in range(length))
