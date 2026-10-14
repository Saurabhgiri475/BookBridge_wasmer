import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_user_aadhaar_last4_user_aadhaar_number_hash_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='referral_code',
            field=models.CharField(blank=True, max_length=12, unique=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='referral_bonus_paid',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='user',
            name='referred_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='referrals', to='accounts.user',
            ),
        ),
    ]
