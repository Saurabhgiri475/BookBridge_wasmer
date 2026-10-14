"""Offers must be at least Rs 1 (validator + DB check constraint). Existing offers below
that are rejected and raised to Rs 1.00 so the constraint can be applied."""
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def repair_bad_offers(apps, schema_editor):
    Offer = apps.get_model('orders', 'Offer')
    Offer.objects.filter(amount__lt=Decimal('1.00')).update(amount=Decimal('1.00'), status='rejected')


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0009_refundrecord'),
    ]

    operations = [
        migrations.RunPython(repair_bad_offers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='offer',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=8, validators=[django.core.validators.MinValueValidator(Decimal('1.00'), message='An offer must be at least ₹1.')]),
        ),
        migrations.AddConstraint(
            model_name='offer',
            constraint=models.CheckConstraint(condition=models.Q(('amount__gte', 1)), name='offer_amount_gte_one'),
        ),
    ]
