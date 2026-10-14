from django.core.management.base import BaseCommand

from partners.assignment import sweep_expired


class Command(BaseCommand):
    help = (
        'Move buyback leads on to the next partner when the current partner has not responded in time. '
        'Schedule every 5-10 minutes (cron / systemd timer / your host\'s scheduler).'
    )

    def handle(self, *args, **options):
        moved = sweep_expired()
        self.stdout.write(f'Reassigned {moved} lead{"s" if moved != 1 else ""}.')
