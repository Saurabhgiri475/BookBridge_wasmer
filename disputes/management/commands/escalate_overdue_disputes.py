"""
Escalate disputes whose seller never replied within the reply window, so a
silent seller can't stall a buyer forever.

    python manage.py escalate_overdue_disputes

Safe to run as often as you like (e.g. hourly from cron / a scheduler) - it only
touches disputes that are still 'open' and past their deadline.
"""
from django.core.management.base import BaseCommand

from disputes.services import escalate_overdue


class Command(BaseCommand):
    help = 'Escalate disputes where the seller did not reply in time.'

    def handle(self, *args, **opts):
        n = escalate_overdue()
        self.stdout.write(self.style.SUCCESS(f'Escalated {n} overdue dispute(s).'))
