from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_user_referral_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_duplicate_flagged',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='user',
            name='duplicate_flag_reason',
            field=models.TextField(blank=True),
        ),
    ]
