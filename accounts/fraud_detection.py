"""
Duplicate-Account Detection.

Compares the identity signal that is directly comparable between accounts:
phone number (exact match) - a strong signal on its own, since phone numbers
are 1:1 with a SIM/person in practice.

Mirrors books/duplicate_check.py and books/fraud_detection.py's pattern: a
small, dependency-free module with one entry point.
"""
PHONE_MATCH_SEVERITY = 'high'


def find_linked_accounts(user):
    """Other User accounts that look like they belong to the same real
    person as `user`. Returns a list of:
        {'user': User, 'reason': str, 'severity': 'high'}
    """
    from .models import User

    if user is None or not getattr(user, 'pk', None):
        return []

    linked = {}

    phone = (user.phone or '').strip()
    if phone:
        for other in User.objects.filter(phone=phone).exclude(pk=user.pk):
            linked[other.pk] = {
                'user': other,
                'reason': 'same phone number',
                'severity': PHONE_MATCH_SEVERITY,
            }

    return list(linked.values())
