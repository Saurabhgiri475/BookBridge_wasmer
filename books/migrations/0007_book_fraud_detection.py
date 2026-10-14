from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0006_savedsearch'),
    ]

    operations = [
        migrations.AlterField(
            model_name='book',
            name='status',
            field=models.CharField(
                choices=[
                    ('available', 'Available'),
                    ('reserved', 'Reserved (offer accepted)'),
                    ('sold', 'Sold'),
                    ('rented', 'Rented Out'),
                    ('under_review', 'Under Review (flagged by Fraud Detection)'),
                    ('removed', 'Removed'),
                ],
                default='available',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='book',
            name='fraud_score',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='book',
            name='fraud_flags',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='book',
            name='fraud_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
