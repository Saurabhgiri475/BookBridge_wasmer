"""
Console-only escape hatch: remove a staff member's 2FA (lost phone AND recovery codes, or a
SECRET_KEY change made the stored secret unreadable). The next time they sign in they must enrol again.

    python manage.py reset_staff_2fa <username>
"""
from django.core.management.base import BaseCommand, CommandError

from accounts.models import StaffRecoveryCode, StaffTOTPDevice, User
from adminpanel.models import AuditLog


class Command(BaseCommand):
    help = "Remove a staff user's two-factor device and recovery codes so they can enrol again."

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, *args, **opts):
        try:
            user = User.objects.get(username=opts['username'])
        except User.DoesNotExist:
            raise CommandError(f"No user named {opts['username']!r}.")
        devices, _ = StaffTOTPDevice.objects.filter(user=user).delete()
        codes, _ = StaffRecoveryCode.objects.filter(user=user).delete()
        AuditLog.record(actor=None, action='staff_2fa_reset', target_repr=user.get_username(), target_id=user.pk,
                        note='Reset from the server console (reset_staff_2fa).')
        self.stdout.write(self.style.SUCCESS(
            f'Removed {devices} device and {codes} recovery code records for {user.username}. They will be asked to enrol again at next sign-in.'))
