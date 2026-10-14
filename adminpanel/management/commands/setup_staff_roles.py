"""
Create (or restore) the standard staff role groups and optionally put someone in one.

    python manage.py setup_staff_roles                       # create any missing role groups
    python manage.py setup_staff_roles --reset               # restore every role to its defaults
    python manage.py setup_staff_roles --grant alice "Finance"
    python manage.py setup_staff_roles --revoke alice "Finance"
"""
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from adminpanel.models import AuditLog
from adminpanel.roles import ROLES, ensure_roles


class Command(BaseCommand):
    help = 'Create the standard staff role groups, or grant/revoke a role for a user.'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true', help='Overwrite existing role groups with the defaults.')
        parser.add_argument('--grant', nargs=2, metavar=('USERNAME', 'ROLE'))
        parser.add_argument('--revoke', nargs=2, metavar=('USERNAME', 'ROLE'))

    def handle(self, *args, **opts):
        written = ensure_roles(reset=opts['reset'])
        self.stdout.write(f"Role groups written: {', '.join(written) or 'none (all already exist)'}")
        for flag in ('grant', 'revoke'):
            if not opts[flag]:
                continue
            username, role = opts[flag]
            if role not in ROLES:
                raise CommandError(f"Unknown role {role!r}. Choose from: {', '.join(ROLES)}")
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(f'No user named {username!r}.')
            group = Group.objects.get(name=role)
            if flag == 'grant':
                user.groups.add(group)
                if not user.is_staff:
                    user.is_staff = True
                    user.save(update_fields=['is_staff'])
            else:
                user.groups.remove(group)
            AuditLog.record(actor=None, action='staff_role_changed', target_repr=user.get_username(), target_id=user.pk,
                            note=f'{flag} role "{role}" (from the console).')
            self.stdout.write(self.style.SUCCESS(f'{flag}ed {role} for {username}.'))
