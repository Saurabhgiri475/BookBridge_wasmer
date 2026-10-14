"""Buyback photos move to private storage (no public URL; served by partners.views.buyback_image).
Run `python manage.py move_private_files` afterwards to relocate existing files."""
import bookmarket.storage
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('partners', '0008_alter_shopkeeperpartner_owner_id_proof_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='buybackrequestimage',
            name='image',
            field=models.ImageField(storage=bookmarket.storage.get_private_storage, upload_to='buyback_requests/'),
        ),
    ]
