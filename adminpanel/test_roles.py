import io

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from adminpanel.models import AuditLog
from adminpanel.roles import CAPABILITIES, ROLES, ensure_roles

PW = 'Str0ng!Pass#123'


def staff_with_role(username, *roles):
    user = User.objects.create_user(username, f'{username}@x.com', PW, is_staff=True)
    for r in roles:
        user.groups.add(Group.objects.get(name=r))
    return user


class RoleSetupTests(TestCase):
    def setUp(self):
        ensure_roles()

    def test_standard_roles_exist_with_expected_capabilities(self):
        self.assertEqual(set(ROLES), set(Group.objects.filter(name__in=ROLES).values_list('name', flat=True)))
        fin = Group.objects.get(name='Finance')
        self.assertEqual({p.codename for p in fin.permissions.filter(codename__startswith='cap_')}, {'cap_finance'})
        sup = Group.objects.get(name='Super Admin')
        self.assertEqual({p.codename for p in sup.permissions.filter(codename__startswith='cap_')}, set(CAPABILITIES))

    def test_every_role_model_permission_resolves(self):
        from adminpanel.roles import _perm
        for name, spec in ROLES.items():
            for cap in spec['caps']:
                self.assertIsNotNone(_perm('adminpanel', cap), cap)
            for app_label, model, actions in spec['models']:
                for a in actions:
                    self.assertIsNotNone(_perm(app_label, f'{a}_{model}'), f'{name}: {a}_{model}')

    def test_ensure_roles_never_overwrites_an_admins_edits_unless_reset(self):
        fin = Group.objects.get(name='Finance')
        fin.permissions.clear()
        ensure_roles()
        self.assertEqual(fin.permissions.count(), 0)
        ensure_roles(reset=True)
        self.assertGreater(fin.permissions.count(), 1)

    def test_command_grants_and_revokes_with_audit_trail(self):
        user = User.objects.create_user('newhire', 'n@x.com', PW)
        call_command('setup_staff_roles', '--grant', 'newhire', 'Finance', stdout=io.StringIO())
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertTrue(user.groups.filter(name='Finance').exists())
        call_command('setup_staff_roles', '--revoke', 'newhire', 'Finance', stdout=io.StringIO())
        self.assertFalse(user.groups.filter(name='Finance').exists())
        self.assertEqual(AuditLog.objects.filter(action='staff_role_changed').count(), 2)


class RoleAccessMatrixTests(TestCase):
    """Which staff pages each role may open. 403 = blocked by role; anything else = allowed through."""

    def setUp(self):
        ensure_roles()
        self.pages = {
            'finance': [reverse('payouts:staff_queue'), reverse('adminpanel:revenue_dashboard')],
            'disputes': [reverse('disputes:staff_queue')],
            'donations': [reverse('donations:staff_queue')],
        }
        self.posts = {
            'moderation': reverse('adminpanel:approve_book', args=[999999]),
            'fraud': reverse('adminpanel:clear_fraud_flag', args=[999999]),
            'partners': reverse('adminpanel:approve_partner', args=[999999]),
        }

    def forbidden(self, client, url, post=False):
        resp = client.post(url) if post else client.get(url)
        return resp.status_code == 403

    def check(self, username, allowed_pages, allowed_posts):
        c = self.client_class()
        c.login(username=username, password=PW)
        for area, urls in self.pages.items():
            for url in urls:
                self.assertEqual(not self.forbidden(c, url), area in allowed_pages, f'{username} GET {url}')
        for area, url in self.posts.items():
            self.assertEqual(not self.forbidden(c, url, post=True), area in allowed_posts, f'{username} POST {url}')

    def test_each_role_reaches_only_its_own_areas(self):
        for role, pages, posts in (
            ('Finance', {'finance'}, set()),
            ('Customer Support', {'disputes', 'donations'}, set()),
            ('Partner Verification', set(), {'partners'}),
            ('Content Moderator', set(), {'moderation'}),
            ('Fraud Team', set(), {'fraud'}),
        ):
            username = role.replace(' ', '').lower()
            staff_with_role(username, role)
            self.check(username, pages, posts)

    def test_staff_without_a_role_can_open_dashboard_but_nothing_specific(self):
        staff_with_role('norole')
        self.check('norole', set(), set())
        c = self.client_class()
        c.login(username='norole', password=PW)
        self.assertEqual(c.get(reverse('adminpanel:dashboard')).status_code, 200)

    def test_superuser_and_super_admin_role_reach_everything(self):
        User.objects.create_superuser('root', 'r@x.com', PW)
        self.check('root', {'finance', 'disputes', 'donations'}, {'moderation', 'fraud', 'partners'})
        staff_with_role('boss', 'Super Admin')
        self.check('boss', {'finance', 'disputes', 'donations'}, {'moderation', 'fraud', 'partners'})

    def test_roles_can_be_combined(self):
        staff_with_role('combo', 'Finance', 'Content Moderator')
        self.check('combo', {'finance'}, {'moderation'})

    def test_dashboard_only_shows_sections_the_role_can_act_on(self):
        staff_with_role('pv', 'Partner Verification')
        staff_with_role('fin', 'Finance')
        c = self.client_class()
        c.login(username='pv', password=PW)
        page = c.get(reverse('adminpanel:dashboard')).content.decode()
        self.assertIn('Pending Partner Applications', page)
        self.assertNotIn('Revenue</a>', page)
        c2 = self.client_class()
        c2.login(username='fin', password=PW)
        page = c2.get(reverse('adminpanel:dashboard')).content.decode()
        self.assertNotIn('Pending Partner Applications', page)
        self.assertIn('Revenue</a>', page)

    def test_partner_documents_need_the_partner_verification_capability(self):
        from partners.test_routing_and_reputation import make_partner
        partner = make_partner('shopowner', status='pending')
        url = reverse('partners:proof_file', args=[partner.pk, 'id'])
        staff_with_role('fin2', 'Finance')
        staff_with_role('pv2', 'Partner Verification')
        c = self.client_class()
        c.login(username='fin2', password=PW)
        self.assertEqual(c.get(url).status_code, 404)
        self.assertEqual(AuditLog.objects.filter(action='view_partner_proof').count(), 0)
        c = self.client_class()
        c.login(username='pv2', password=PW)
        self.assertEqual(c.get(url).status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action='view_partner_proof').count(), 1)


class DjangoAdminRoleTests(TestCase):
    def setUp(self):
        ensure_roles()

    def _action_names(self, username):
        """Actions the Django admin would offer this user on the return-request list."""
        from django.contrib import admin
        from django.test import RequestFactory

        from orders.models import ReturnRequest
        request = RequestFactory().get('/admin/orders/returnrequest/')
        request.user = User.objects.get(username=username)
        model_admin = admin.site._registry[ReturnRequest]
        return {name for name in model_admin.get_actions(request) if name in ('approve_and_refund', 'refund_to_wallet')}

    def test_only_finance_may_run_refund_actions(self):
        staff_with_role('fin3', 'Finance')
        staff_with_role('sup3', 'Customer Support')
        self.assertEqual(self._action_names('fin3'), {'approve_and_refund', 'refund_to_wallet'})
        self.assertEqual(self._action_names('sup3'), set())    # can open return requests but cannot refund them
        url = reverse('admin:orders_returnrequest_changelist')
        sup = self.client_class()
        sup.login(username='sup3', password=PW)
        self.assertEqual(sup.get(url).status_code, 200)
        User.objects.create_superuser('root4', 'r4@x.com', PW)
        self.assertEqual(self._action_names('root4'), {'approve_and_refund', 'refund_to_wallet'})

    def test_staff_with_no_role_sees_no_models_in_admin(self):
        staff_with_role('plain3')
        c = self.client_class()
        c.login(username='plain3', password=PW)
        self.assertEqual(c.get(reverse('admin:orders_returnrequest_changelist')).status_code, 403)
        self.assertEqual(c.get(reverse('admin:payouts_payout_changelist')).status_code, 403)

    def test_moderator_cannot_open_finance_or_account_tables(self):
        staff_with_role('mod3', 'Content Moderator')
        c = self.client_class()
        c.login(username='mod3', password=PW)
        self.assertEqual(c.get(reverse('admin:books_book_changelist')).status_code, 200)
        self.assertEqual(c.get(reverse('admin:payouts_payout_changelist')).status_code, 403)
        self.assertEqual(c.get(reverse('admin:accounts_user_changelist')).status_code, 403)
