from django.conf import settings


def support_contact(request):
    """Support phone/email for every template (footer, login, reset, contact page)."""
    phone = (settings.SUPPORT_PHONE or '').strip().replace(' ', '')
    digits = phone.lstrip('+')
    if len(digits) == 10:
        digits = '91' + digits           # Indian mobile without country code
    nice = f'+{digits[:2]} {digits[2:7]} {digits[7:]}' if len(digits) == 12 else phone
    return {
        'support_phone_display': nice if phone else '',
        'support_phone_tel': f'+{digits}' if phone else '',
        'support_email': settings.SUPPORT_EMAIL,
    }
