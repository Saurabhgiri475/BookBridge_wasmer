"""Chat photos move to private storage (no public URL; served by chatapp.views.chat_image).
Run `python manage.py move_private_files` afterwards to relocate existing files."""
import bookmarket.storage
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatapp', '0002_conversation_buyback_alter_conversation_book_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='message',
            name='image',
            field=models.ImageField(blank=True, null=True, storage=bookmarket.storage.get_private_storage, upload_to='chat_images/'),
        ),
    ]
