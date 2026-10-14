from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0002_payment_gateway_shiprocket'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='payment_method',
            field=models.CharField(
                choices=[
                    ('cod', 'Cash on Delivery'), ('upi', 'UPI'), ('card', 'Card'), ('wallet', 'Wallet'),
                    ('bb_wallet', 'BookBridge Wallet'),
                ],
                default='cod', max_length=20,
            ),
        ),
    ]
