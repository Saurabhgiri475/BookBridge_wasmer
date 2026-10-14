from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('books', '0013_digital_file_content_check'),
    ]

    operations = [
        migrations.AddField(
            model_name='book',
            name='payment_reserved_by',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='book',
            name='payment_reserved_until',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
