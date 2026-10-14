from django.core.management.base import BaseCommand

from orders.models import Order
from payouts.models import Payout
from payouts.services import ensure_payout


class Command(BaseCommand):
    help = (
        'Create Payout rows for orders that were delivered before the payouts feature existed. '
        'By default they start as pending, so staff review them in the queue. If you have already '
        'paid those sellers outside the app, use --mark-paid so nobody is paid twice.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--mark-paid', action='store_true',
            help='Mark the created payouts as paid (no UTR is known for them) instead of pending.',
        )
        parser.add_argument('--dry-run', action='store_true', help='Only report how many would be created.')

    def handle(self, *args, **opts):
        orders = (
            Order.objects.filter(status='delivered', payment__status='released', payout__isnull=True)
            .select_related('book__seller', 'payment').order_by('created_at')
        )
        total = orders.count()
        if opts['dry_run']:
            self.stdout.write(f'{total} delivered order(s) have no payout row.')
            return
        for order in orders.iterator():
            payout = ensure_payout(order)
            # Backdate to when the order last changed, so old sales land in the right statement month.
            payout.released_at = order.updated_at
            update = ['released_at']
            if opts['mark_paid']:
                payout.status = 'paid'
                payout.paid_at = order.updated_at
                payout.note = 'Backfilled as already paid (paid before payout tracking existed; no UTR on record).'
                update += ['status', 'paid_at', 'note']
            payout.save(update_fields=update)
        self.stdout.write(self.style.SUCCESS(f'Created {total} payout row(s).'))
