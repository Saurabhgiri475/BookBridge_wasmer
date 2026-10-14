"""Create the database cache table when CACHE_BACKEND=db.

Hosts such as Wasmer Edge run `migrate` automatically but not `createcachetable`. The app's
rate limiting needs a shared cache in production, so with CACHE_BACKEND=db this migration
creates the table for you. It does nothing for any other cache backend, and is safe to re-run.
"""
from django.conf import settings
from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    if any('DatabaseCache' in c.get('BACKEND', '') for c in settings.CACHES.values()):
        call_command('createcachetable', database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0009_remove_aadhaar_verification')]
    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]
