import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('books', '0005_book_is_rentable_and_rented_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='SavedSearch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('query', models.CharField(blank=True, help_text='Free-text keywords, same as the search box.', max_length=255)),
                ('board', models.CharField(blank=True, max_length=100)),
                ('university', models.CharField(blank=True, max_length=150)),
                ('semester', models.CharField(blank=True, max_length=20)),
                ('condition', models.CharField(
                    blank=True,
                    choices=[('like_new', 'Like New'), ('excellent', 'Excellent'), ('good', 'Good'), ('fair', 'Fair'), ('old', 'Old')],
                    max_length=20,
                )),
                ('city', models.CharField(blank=True, max_length=100)),
                ('min_price', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                ('max_price', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True)),
                ('listing_type', models.CharField(
                    blank=True,
                    choices=[('physical', 'Physical Book'), ('digital', 'Digital Book / Material')],
                    max_length=10,
                )),
                ('digital_type', models.CharField(
                    blank=True,
                    choices=[
                        ('pdf_notes', 'PDF Notes'),
                        ('handwritten_notes', 'Handwritten Notes'),
                        ('question_paper', 'Question Papers'),
                        ('lab_manual', 'Lab Manuals'),
                    ],
                    max_length=20,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_notified_at', models.DateTimeField(blank=True, null=True)),
                ('category', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='books.category')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='saved_searches', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
