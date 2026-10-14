import logging
import os

from django.apps import AppConfig, apps
from django.db.models.signals import post_migrate

logger = logging.getLogger(__name__)


def _create_standard_roles(sender, **kwargs):
    """Create the standard staff role groups after `migrate`.

    Runs on the LAST app's post_migrate signal, not adminpanel's own: Django creates each app's permissions during
    that app's own signal, so running earlier made roles on a fresh database miss every permission of the apps
    migrated after adminpanel (partners, donations, payouts, disputes).

    Existing groups are never overwritten (an admin may have tuned them). To restore the defaults - e.g. roles that an
    older deploy created incomplete - set RESET_STAFF_ROLES=1 for one deploy, then remove it.
    """
    last = [c for c in apps.get_app_configs() if c.models_module][-1]
    if sender is not last:
        return
    from .roles import ensure_roles
    reset = os.environ.get('RESET_STAFF_ROLES', '').strip().lower() in ('1', 'true', 'yes')
    written = ensure_roles(reset=reset)
    if written:
        logger.info('Staff role groups %s: %s', 'reset' if reset else 'created', ', '.join(written))


class AdminpanelConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'adminpanel'

    def ready(self):
        post_migrate.connect(_create_standard_roles, dispatch_uid='adminpanel.create_standard_roles')
