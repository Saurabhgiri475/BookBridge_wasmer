import logging
import os

from django.apps import AppConfig
from django.db.models.signals import post_migrate

logger = logging.getLogger(__name__)


def bootstrap_admin(sender, **kwargs):
    """Runs once per `migrate` (on the last app's post_migrate signal): create the first superuser if asked to."""
    from django.apps import apps
    last = [c for c in apps.get_app_configs() if c.models_module][-1]
    if sender is last:
        reset_database_once()
        bootstrap_superuser()


RESET_CONFIRM_PHRASE = 'DELETE-ALL-DATA'


def reset_database_once():
    """One-off "fresh start" for hosts with no shell (Wasmer Edge): wipe every row, keep the tables.

    Only runs when RESET_DATABASE is exactly 'DELETE-ALL-DATA'. It is guarded so it fires ONCE per value: a marker row
    is written to django_migrations (which `flush` never touches) before the wipe, so later deploys with the variable
    still set do nothing. To wipe again, change the value to 'DELETE-ALL-DATA:2' (any suffix after the colon).
    Files already uploaded to S3/R2 buckets or a volume are NOT deleted.
    """
    value = os.environ.get('RESET_DATABASE', '').strip()
    if value != RESET_CONFIRM_PHRASE and not value.startswith(RESET_CONFIRM_PHRASE + ':'):
        return
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    marker = 'reset_' + value.replace(':', '_')[:240]
    qs = MigrationRecorder(connection).migration_qs
    if qs.filter(app='_database_reset', name=marker).exists():
        return  # already done for this value (also stops the recursion from flush's own post_migrate)
    qs.create(app='_database_reset', name=marker)
    logger.warning('RESET_DATABASE: wiping ALL data now. Remove the RESET_DATABASE variable after this deploy.')
    call_command('flush', interactive=False, verbosity=0)
    logger.warning('RESET_DATABASE: done - all old data was wiped.')


def bootstrap_superuser():
    """Create the first admin from environment variables (hosts with no shell, e.g. Wasmer Edge).

    Runs after every `migrate`. Needs DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_EMAIL and
    DJANGO_SUPERUSER_PASSWORD. It only ever CREATES a missing account - an existing user (or an
    existing account with that email) is never modified, so it cannot reset a password or promote
    anyone. Remove the three variables after the first successful login. RESET_DATABASE (below) runs first, so a reset followed by a new admin works in one deploy.
    """
    username = os.environ.get('DJANGO_SUPERUSER_USERNAME', '').strip()
    email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '').strip()
    password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '')
    if not (username and email and password):
        return
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    User = get_user_model()
    if User.objects.filter(username__iexact=username).exists() or User.objects.filter(email__iexact=email).exists():
        logger.info('bootstrap_superuser: an account with that username/email already exists - nothing changed.')
        return
    try:
        validate_password(password)
    except ValidationError as exc:
        logger.error('bootstrap_superuser: DJANGO_SUPERUSER_PASSWORD rejected (%s) - admin not created.', ' '.join(exc.messages))
        return
    User.objects.create_superuser(username=username, email=email, password=password, email_verified=True)
    logger.warning('bootstrap_superuser: created superuser %r. Remove the DJANGO_SUPERUSER_* variables now.', username)


class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self):
        post_migrate.connect(bootstrap_admin, dispatch_uid='accounts.bootstrap_admin')
        from django.contrib.auth.signals import user_logged_in

        from . import twofactor

        def forget_2fa_on_login(sender, request, user, **kwargs):
            # Every new login starts unverified, even if the session was reused.
            if request is not None and hasattr(request, 'session'):
                twofactor.clear_verified(request)

        user_logged_in.connect(forget_2fa_on_login, dispatch_uid='accounts.forget_2fa_on_login')
