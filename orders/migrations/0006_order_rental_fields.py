from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0005_alter_order_delivery_address_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='order_type',
            field=models.CharField(
                choices=[('purchase', 'Purchase'), ('rental', 'Rental')],
                default='purchase',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='rental_days',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='rental_due_date',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
