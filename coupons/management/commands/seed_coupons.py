from django.core.management.base import BaseCommand
from coupons.models import Coupon


class Command(BaseCommand):
    help = 'Creates a few sample coupons for local testing (idempotent - safe to re-run).'

    def handle(self, *args, **options):
        samples = [
            dict(
                code='WELCOME50', description='Flat ₹50 off for new buyers',
                discount_type='flat', discount_value=50, min_order_amount=200,
                first_order_only=True, usage_limit_per_user=1,
            ),
            dict(
                code='SAVE10', description='10% off, up to ₹100',
                discount_type='percent', discount_value=10, max_discount_amount=100,
                min_order_amount=100, usage_limit_per_user=3,
            ),
            dict(
                code='FEST20', description='Festive sale - 20% off, up to ₹250',
                discount_type='percent', discount_value=20, max_discount_amount=250,
                min_order_amount=300, usage_limit_total=500, usage_limit_per_user=1,
            ),
        ]
        for data in samples:
            coupon, created = Coupon.objects.get_or_create(code=data['code'], defaults=data)
            action = 'Created' if created else 'Already exists'
            self.stdout.write(f'{action}: {coupon.code} ({coupon.description})')
