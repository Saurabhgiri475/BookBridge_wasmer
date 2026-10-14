from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('payouts', '0001_payouts'),
    ]

    operations = [
        migrations.AddField(model_name='payoutprofile', name='verified_upi', field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name='payoutprofile', name='verified_name', field=models.CharField(blank=True, help_text='Account-holder name the UPI app showed.', max_length=120)),
        migrations.AddField(model_name='payoutprofile', name='name_check_override', field=models.BooleanField(default=False, help_text="Staff accepted this name even though it did not match the seller's account name.")),
        migrations.AddField(model_name='payoutprofile', name='name_checked_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='payoutprofile', name='name_checked_by', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='upi_name_checks', to=settings.AUTH_USER_MODEL)),
    ]
