# Generated for real Razorpay + Shiprocket integration

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='razorpay_order_id',
            field=models.CharField(blank=True, db_index=True, max_length=100),
        ),
        migrations.AddField(
            model_name='payment',
            name='razorpay_payment_id',
            field=models.CharField(blank=True, db_index=True, max_length=100),
        ),
        migrations.AddField(
            model_name='payment',
            name='razorpay_signature',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='payment',
            name='status',
            field=models.CharField(
                choices=[
                    ('created', 'Razorpay Order Created'), ('pending', 'Pending'),
                    ('escrow_held', 'Held in Escrow'), ('released', 'Released to Seller'),
                    ('refunded', 'Refunded'), ('failed', 'Failed'),
                ],
                default='pending', max_length=20,
            ),
        ),
        migrations.RemoveField(
            model_name='shipment',
            name='partner',
        ),
        migrations.AddField(
            model_name='shipment',
            name='shiprocket_order_id',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='shipment',
            name='shiprocket_shipment_id',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='shipment',
            name='courier_name',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='shipment',
            name='tracking_history',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='shipment',
            name='tracking_synced_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='shipment',
            name='tracking_id',
            field=models.CharField(
                blank=True, max_length=40, null=True, unique=True,
                help_text='AWB code assigned by the courier via Shiprocket',
            ),
        ),
        migrations.AlterField(
            model_name='shipment',
            name='status',
            field=models.CharField(
                choices=[
                    ('pickup_pending', 'Pickup Pending'), ('picked_up', 'Picked Up'),
                    ('in_transit', 'In Transit'), ('out_for_delivery', 'Out for Delivery'),
                    ('delivered', 'Delivered'), ('failed', 'Delivery Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pickup_pending', max_length=20,
            ),
        ),
    ]
