import django.db.models.deletion
from django.db import migrations, models

DEFAULT_CATEGORIES = [
    ('CBSE', 'academic'), ('ICSE', 'academic'), ('State Board', 'academic'), ('University', 'academic'),
    ('JEE', 'competitive'), ('NEET', 'competitive'), ('UPSC', 'competitive'), ('SSC', 'competitive'),
    ('Banking', 'competitive'), ('CAT', 'competitive'), ('GATE', 'competitive'),
    ('Engineering', 'college'), ('Medical', 'college'), ('BCA', 'college'), ('MCA', 'college'),
    ('B.Tech', 'college'), ('MBA', 'college'), ('BBA', 'college'), ('B.Sc', 'college'), ('M.Sc', 'college'),
    ('Novels', 'others'), ('Comics', 'others'), ("Children's Books", 'others'), ('Magazines', 'others'),
    ('Rare Books', 'others'),
]


def seed_default_categories(apps, schema_editor):
    Category = apps.get_model('books', 'Category')
    for name, group in DEFAULT_CATEGORIES:
        Category.objects.get_or_create(name=name, defaults={'group': group})


def noop_reverse(apps, schema_editor):
    # Deliberately not deleting categories on reverse - a seller may have
    # already listed real books against them by the time anyone rolls back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('books', '0002_book_pickup_latitude_longitude'),
    ]

    operations = [
        migrations.AlterField(
            model_name='book',
            name='category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                     related_name='books', to='books.category'),
        ),
        migrations.RunPython(seed_default_categories, noop_reverse),
    ]
