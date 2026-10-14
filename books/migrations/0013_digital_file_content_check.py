import books.file_checks
import books.models
import bookmarket.storage
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0012_price_rules'),
    ]

    operations = [
        migrations.AlterField(
            model_name='book',
            name='digital_file',
            field=models.FileField(blank=True, help_text='The PDF/scanned file buyers receive by email + download link after payment. Max 25 MB; PDF, Word, ZIP or images.', null=True, storage=bookmarket.storage.get_private_storage, upload_to='digital_books/%Y/%m/', validators=[django.core.validators.FileExtensionValidator(allowed_extensions=['pdf', 'doc', 'docx', 'zip', 'png', 'jpg', 'jpeg']), books.models.validate_digital_file_size, books.file_checks.validate_digital_file_content]),
        ),
    ]
