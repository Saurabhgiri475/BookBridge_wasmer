from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0004_book_digital_file_book_digital_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='book',
            name='is_rentable',
            field=models.BooleanField(
                default=False,
                help_text='Physical books only. Lets buyers rent this copy (7/15/30 days) instead of '
                          'buying it, priced as a % of the buy price above.',
            ),
        ),
        migrations.AlterField(
            model_name='book',
            name='status',
            field=models.CharField(
                choices=[
                    ('available', 'Available'),
                    ('reserved', 'Reserved (offer accepted)'),
                    ('sold', 'Sold'),
                    ('rented', 'Rented Out'),
                    ('removed', 'Removed'),
                ],
                default='available',
                max_length=20,
            ),
        ),
    ]
