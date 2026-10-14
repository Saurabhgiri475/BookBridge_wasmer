from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User


class Command(BaseCommand):
    help = (
        'Deletes signups that never confirmed their email code (signup_pending) and are older '
        'than --hours, so abandoned or mistyped signups do not hold on to usernames forever. '
        'Any partner application submitted with them goes too. Run it daily from cron.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=48, help='Age in hours before an unverified signup is deleted (default 48).')
        parser.add_argument('--dry-run', action='store_true', help='Only report what would be deleted.')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=options['hours'])
        stale = User.objects.filter(signup_pending=True, is_active=False, date_joined__lt=cutoff)
        count = stale.count()
        if options['dry_run']:
            self.stdout.write(f'Would delete {count} unverified signup(s) older than {options["hours"]}h.')
            return
        stale.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {count} unverified signup(s) older than {options["hours"]}h.'))
