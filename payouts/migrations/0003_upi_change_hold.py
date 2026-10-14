from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('payouts', '0002_upi_name_check'),
    ]

    operations = [
        migrations.AddField(model_name='payoutprofile', name='upi_changed_at', field=models.DateTimeField(blank=True, null=True, help_text='When the UPI ID last changed (starts the payout hold).')),
        migrations.AddField(model_name='payoutprofile', name='hold_override_upi', field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name='payoutprofile', name='hold_override_reason', field=models.CharField(blank=True, max_length=300)),
        migrations.AddField(model_name='payoutprofile', name='hold_override_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='payoutprofile', name='hold_override_by', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='upi_hold_overrides', to=settings.AUTH_USER_MODEL)),
    ]
