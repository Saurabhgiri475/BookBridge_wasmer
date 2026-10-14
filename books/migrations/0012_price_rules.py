"""Enforce price >= 1, original_price >= 0 and quantity >= 1 (validators + DB check constraints).

Rows that already break those rules would make the constraints fail to apply, so they
are repaired first: a listing priced below Rs 1 is pulled off the market ('under_review')
and set to Rs 1.00 so a human must re-price it; a bad quantity becomes 1; a negative
original price is cleared."""
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def repair_bad_rows(apps, schema_editor):
    Book = apps.get_model('books', 'Book')
    Book.objects.filter(price__lt=Decimal('1.00')).update(price=Decimal('1.00'), status='under_review')
    Book.objects.filter(quantity__lt=1).update(quantity=1)
    Book.objects.filter(original_price__lt=0).update(original_price=None)


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0011_alter_book_digital_file'),
    ]

    operations = [
        migrations.RunPython(repair_bad_rows, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='book',
            name='original_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0'))]),
        ),
        migrations.AlterField(
            model_name='book',
            name='price',
            field=models.DecimalField(decimal_places=2, max_digits=8, validators=[django.core.validators.MinValueValidator(Decimal('1.00'), message='The price must be at least ₹1.')]),
        ),
        migrations.AlterField(
            model_name='book',
            name='quantity',
            field=models.PositiveIntegerField(default=1, validators=[django.core.validators.MinValueValidator(1)]),
        ),
        migrations.AddConstraint(
            model_name='book',
            constraint=models.CheckConstraint(condition=models.Q(('price__gte', 1)), name='book_price_gte_one'),
        ),
        migrations.AddConstraint(
            model_name='book',
            constraint=models.CheckConstraint(condition=models.Q(('original_price__isnull', True), ('original_price__gte', 0), _connector='OR'), name='book_original_price_gte_zero'),
        ),
        migrations.AddConstraint(
            model_name='book',
            constraint=models.CheckConstraint(condition=models.Q(('quantity__gte', 1)), name='book_quantity_gte_one'),
        ),
    ]
