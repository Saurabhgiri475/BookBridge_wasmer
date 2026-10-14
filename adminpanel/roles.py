"""
Staff roles. A role is a Django Group; what it can do is the set of permissions on that group.

Two layers, both enforced on the server:
  1. Capability permissions (adminpanel.cap_*) guard the Staff Dashboard pages - see
     adminpanel.views.staff_required('cap_...') - and the sensitive Django-admin actions (refunds).
  2. Ordinary Django model permissions (view/change on specific models) decide which tables a
     person can open in the Django admin at all.

Superusers pass every check. Give everyone else exactly one role (or a combination) in
Django admin -> Users -> Groups, instead of an unrestricted is_superuser account.
"""
from django.contrib.auth.models import Group, Permission

CAPABILITIES = {
    'cap_moderation': 'Review, approve and remove listings and buyer reports',
    'cap_fraud': 'Clear fraud flags and duplicate-account flags',
    'cap_partners': 'Review Shopkeeper Partner applications and open their ID documents',
    'cap_finance': 'Seller payouts, revenue figures and issuing refunds',
    'cap_disputes': 'Handle buyer disputes and return requests',
    'cap_donations': 'Handle book donations and certificates',
    'cap_audit': 'Read the full staff audit log',
}

_VIEW = ('view',)
_EDIT = ('view', 'change')

ROLES = {
    'Super Admin': {
        'caps': list(CAPABILITIES),
        'models': [],  # superusers see everything; this group only carries every capability
    },
    'Finance': {
        'caps': ['cap_finance'],
        'models': [('orders', 'order', _VIEW), ('orders', 'payment', _VIEW), ('orders', 'shipment', _VIEW),
                   ('orders', 'returnrequest', _EDIT), ('orders', 'refundrecord', _VIEW), ('wallet', 'wallet', _VIEW), ('wallet', 'wallettransaction', _VIEW),
                   ('payouts', 'payout', _EDIT), ('payouts', 'payoutprofile', _VIEW)],
    },
    'Fraud Team': {
        'caps': ['cap_fraud', 'cap_audit'],
        'models': [('accounts', 'user', _EDIT), ('books', 'book', _EDIT), ('adminpanel', 'auditlog', _VIEW)],
    },
    'Customer Support': {
        'caps': ['cap_disputes', 'cap_donations'],
        'models': [('orders', 'order', _VIEW), ('orders', 'returnrequest', _EDIT), ('disputes', 'dispute', _EDIT),
                   ('donations', 'donation', _EDIT), ('accounts', 'user', _VIEW)],
    },
    'Partner Verification': {
        'caps': ['cap_partners'],
        'models': [('partners', 'shopkeeperpartner', _EDIT), ('partners', 'partnerreview', _EDIT)],
    },
    'Content Moderator': {
        'caps': ['cap_moderation'],
        'models': [('books', 'book', _EDIT), ('books', 'listingreport', _EDIT)],
    },
}


def _perm(app_label, codename):
    return Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()


def ensure_roles(reset=False):
    """Create the standard role groups. Existing groups are left exactly as an admin tuned them,
    unless reset=True, which restores the defaults. Returns the names of groups that were (re)written."""
    written = []
    for name, spec in ROLES.items():
        group, created = Group.objects.get_or_create(name=name)
        if not (created or reset):
            continue
        perms = [p for p in (_perm('adminpanel', cap) for cap in spec['caps']) if p]
        for app_label, model, actions in spec['models']:
            perms += [p for p in (_perm(app_label, f'{a}_{model}') for a in actions) if p]
        group.permissions.set(perms)
        written.append(name)
    return written


def has_capability(user, capability):
    return bool(user.is_authenticated and user.is_active and user.has_perm(f'adminpanel.{capability}'))
