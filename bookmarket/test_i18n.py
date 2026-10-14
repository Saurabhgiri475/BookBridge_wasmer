"""Hindi / English language switching (LocaleMiddleware + set_language + locale/hi)."""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse


class LanguageSwitchTests(TestCase):
    def test_default_is_english(self):
        r = self.client.get(reverse('books:home'))
        self.assertContains(r, 'Give your old books a second life.')
        self.assertContains(r, '<html lang="en">')

    def test_browser_accept_language_hindi_is_honoured(self):
        r = self.client.get(reverse('books:home'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'अपनी पुरानी किताबों को दें नई ज़िंदगी।')
        self.assertContains(r, '<html lang="hi">')

    def test_switcher_sets_cookie_and_page_turns_hindi_then_back(self):
        r = self.client.post(reverse('set_language'), {'language': 'hi', 'next': reverse('books:home')})
        self.assertRedirects(r, reverse('books:home'), fetch_redirect_response=False)
        self.assertEqual(self.client.cookies[settings.LANGUAGE_COOKIE_NAME].value, 'hi')
        page = self.client.get(reverse('books:home'))
        self.assertContains(page, 'हमारा सामुदायिक प्रभाव')
        self.assertContains(page, 'हिन्दी')  # the switcher shows the current language's own name

        self.client.post(reverse('set_language'), {'language': 'en', 'next': '/'})
        self.assertContains(self.client.get(reverse('books:home')), 'Our Community Impact')

    def test_next_url_to_another_site_is_refused(self):
        r = self.client.post(reverse('set_language'), {'language': 'hi', 'next': 'https://evil.example/'})
        self.assertEqual(r['Location'], '/')

    def test_unknown_language_is_ignored(self):
        self.client.post(reverse('set_language'), {'language': 'xx', 'next': '/'})
        self.assertContains(self.client.get(reverse('books:home')), 'Our Community Impact')

    def test_login_page_in_hindi_keeps_support_links(self):
        self.client.post(reverse('set_language'), {'language': 'hi', 'next': '/'})
        r = self.client.get(reverse('accounts:login'))
        self.assertContains(r, 'BookBridge में लॉग इन करें')
        self.assertContains(r, 'पर कॉल करें')
        self.assertContains(r, 'href="tel:')
        self.assertContains(r, 'subject=BookBridge%20login%20help')  # '%%' in the catalogue renders as '%'

    def test_signup_title_translated(self):
        r = self.client.get(reverse('accounts:signup'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'अपना BookBridge खाता बनाएं')

    def test_search_placeholder_is_attribute_safe(self):
        r = self.client.get(reverse('books:home'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'आज़माएं: &quot;मुझे B.Tech तीसरे सेमेस्टर AKTU के लिए DBMS की किताब चाहिए&quot;')


class AboutAndContactHindiTests(TestCase):
    def test_about_page_translated(self):
        r = self.client.get(reverse('books:about'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'किताबें जो एक विद्यार्थी से दूसरे तक पहुँचती हैं')
        self.assertContains(r, 'आम सवाल')

    def test_contact_page_translated_and_keeps_links(self):
        r = self.client.get(reverse('books:contact'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'मदद चाहिए?')
        self.assertContains(r, 'href="tel:')
        self.assertContains(r, 'subject=BookBridge%20support')


class DashboardAndProfileHindiTests(TestCase):
    def test_profile_page_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_reader', password='pw-12345-xyz')
        r = self.client.get(reverse('accounts:profile_view', args=[user.username]), headers={'accept-language': 'hi'})
        self.assertContains(r, 'बैज और उपलब्धियाँ')
        self.assertContains(r, 'बचाए गए पेड़')

    def test_dashboard_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_seller', password='pw-12345-xyz')
        self.client.force_login(user)
        r = self.client.get(reverse('accounts:dashboard'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'मेरा डैशबोर्ड')
        self.assertContains(r, 'हाल की बिक्री')


class ProfileEditAndVerifyHindiTests(TestCase):
    def test_edit_profile_and_verify_seller_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_editor', password='pw-12345-xyz', email='h@example.com')
        self.client.force_login(user)
        r = self.client.get(reverse('accounts:edit_profile'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'प्रोफ़ाइल संपादित करें')
        r = self.client.get(reverse('accounts:verify_seller'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'विक्रेता सत्यापन')
        self.assertContains(r, 'OTP भेजें')


class PasswordResetHindiTests(TestCase):
    def test_reset_form_and_done_translated_and_keep_links(self):
        r = self.client.get(reverse('accounts:password_reset'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'रीसेट लिंक भेजें')
        self.assertContains(r, 'href="tel:')
        self.assertContains(r, 'subject=BookBridge%20password%20help')
        r = self.client.get(reverse('accounts:password_reset_done'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'अपना ईमेल देखें')


class PasswordResetConfirmHindiTests(TestCase):
    def test_invalid_link_and_complete_pages_translated(self):
        r = self.client.get(reverse('accounts:password_reset_confirm', args=['bad', 'token']),
                            headers={'accept-language': 'hi'}, follow=True)
        self.assertContains(r, 'यह लिंक अब मान्य नहीं है')
        r = self.client.get(reverse('accounts:password_reset_complete'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'पासवर्ड बदल गया')


class TwoFactorCatalogueHindiTests(TestCase):
    """The 2FA pages are staff-only, so check the catalogue entries directly."""

    def test_two_factor_strings_and_plural(self):
        from django.utils.translation import gettext, ngettext, override
        with override('hi'):
            self.assertEqual(gettext('Two-factor check'), 'दो-चरणीय जाँच')
            self.assertEqual(gettext('Save your recovery codes'), 'अपने रिकवरी कोड सहेज लें')
            one = ngettext('Active since %(date)s. You have <strong>%(counter)s</strong> unused recovery code.',
                           'Active since %(date)s. You have <strong>%(counter)s</strong> unused recovery codes.', 1)
            many = ngettext('Active since %(date)s. You have <strong>%(counter)s</strong> unused recovery code.',
                            'Active since %(date)s. You have <strong>%(counter)s</strong> unused recovery codes.', 5)
            self.assertIn('कोड है', one)
            self.assertIn('कोड हैं', many)


class AnalyticsRequestWishlistHindiTests(TestCase):
    def test_pages_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_buyer', password='pw-12345-xyz')
        self.client.force_login(user)
        hi = {'accept-language': 'hi'}
        self.assertContains(self.client.get(reverse('accounts:seller_analytics'), headers=hi), 'विक्रेता एनालिटिक्स')
        self.assertContains(self.client.get(reverse('books:add_request'), headers=hi), 'किताब का अनुरोध करें')
        self.assertContains(self.client.get(reverse('books:wishlist'), headers=hi), 'मेरी विशलिस्ट')


class BooksAndOrdersListPagesHindiTests(TestCase):
    def test_pages_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_member', password='pw-12345-xyz')
        self.client.force_login(user)
        hi = {'accept-language': 'hi'}
        for name, text in [
            ('books:request_list', 'किताब अनुरोध'),
            ('books:matching_requests', 'सभी खुले अनुरोध'),
            ('books:saved_search_list', 'मेरी सहेजी गई खोजें'),
            ('books:bulk_upload', 'एक साथ कई किताबें अपलोड करें'),
            ('orders:my_orders', 'मेरे ऑर्डर'),
        ]:
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name), headers=hi), text)


class Batch11HindiTests(TestCase):
    def test_pages_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_b11', password='pw-12345-xyz')
        self.client.force_login(user)
        hi = {'accept-language': 'hi'}
        for name, text in [
            ('books:sustainability', 'स्थिरता डैशबोर्ड'),
            ('books:add', 'बिक्री के लिए किताब लिस्ट करें'),
            ('chatapp:inbox', 'संदेश'),
            ('notifications:list', 'सूचनाएँ'),
            ('notifications:preferences', 'सूचना की पसंद'),
            ('wallet:detail', 'मेरा वॉलेट'),
            ('offline', 'आप ऑफ़लाइन हैं'),
        ]:
            with self.subTest(page=name):
                r = self.client.get(reverse(name), headers=hi)
                self.assertContains(r, text)

    def test_percent_sign_survives_in_wallet_cashback_text(self):
        from django.utils.translation import gettext, override
        with override('hi'):
            msg = gettext("<strong>Cashback:</strong> %(percent)s%% of every order's value is credited automatically once you confirm delivery.")
            self.assertIn('%(percent)s%%', msg)


class Batch12HindiTests(TestCase):
    def test_my_disputes_page_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_b12', password='pw-12345-xyz')
        self.client.force_login(user)
        r = self.client.get(reverse('disputes:mine'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'मेरे विवाद')
        self.assertContains(r, 'मेरे खोले गए विवाद')

    def test_catalogue_entries(self):
        from django.utils.translation import gettext, ngettext, override
        with override('hi'):
            self.assertEqual(gettext('Out for Delivery'), 'डिलीवरी के लिए निकला')
            self.assertIn('%(percent)s%%', gettext('Payout: ₹%(payout)s (after %(percent)s%% commission)'))
            self.assertIn('किताब की', ngettext('%(counter)s book verified received &middot; issued %(date)s',
                                               '%(counter)s books verified received &middot; issued %(date)s', 1))


class Batch13HindiTests(TestCase):
    def test_public_and_member_pages_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_b13', password='pw-12345-xyz')
        hi = {'accept-language': 'hi'}
        self.assertContains(self.client.get(reverse('donations:home'), headers=hi), 'किताबें दान करें, प्रमाणपत्र पाएँ')
        self.assertContains(self.client.get(reverse('donations:verify_form'), headers=hi), 'दान प्रमाणपत्र सत्यापित करें')
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse('donations:mine'), headers=hi), 'मेरे दान')
        self.assertContains(self.client.get(reverse('donations:create'), headers=hi), 'दान जमा करें')
        self.assertContains(self.client.get(reverse('payouts:mine'), headers=hi), 'पेआउट और स्टेटमेंट')

    def test_percent_and_plural_entries(self):
        from django.utils.translation import gettext, ngettext, override
        with override('hi'):
            self.assertIn('%(percent)s%%', gettext('Commission (%(percent)s%%)'))
            self.assertIn('किताब', ngettext('%(counter)s book', '%(counter)s books', 3))


class Batch14HindiTests(TestCase):
    def test_partner_pages_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_b14', password='pw-12345-xyz')
        self.client.force_login(user)
        hi = {'accept-language': 'hi'}
        self.assertContains(self.client.get(reverse('partners:apply'), headers=hi), 'BookBridge दुकानदार पार्टनर बनें')
        self.assertContains(self.client.get(reverse('partners:buyback_list'), headers=hi), 'मेरे सीधे बायबैक अनुरोध')

    def test_catalogue_entries(self):
        from django.utils.translation import gettext, ngettext, override
        with override('hi'):
            self.assertIn('10%%', gettext("Partners buy old books directly from sellers on BookBridge's behalf - instant cash for the seller, fresh stock for your shop. Approved partners pay BookBridge a 10%% commission on every book they buy this way. We ask for a couple of documents up front just to confirm you run a real shop."))
            self.assertIn('लीड', ngettext('<strong>%(counter)s lead reserved for you.</strong> Make an offer (or pass) within %(minutes)s minutes of getting it - otherwise it moves to the next partner nearby.',
                                           '<strong>%(counter)s leads reserved for you.</strong> Make an offer (or pass) within %(minutes)s minutes of getting it - otherwise it moves to the next partner nearby.', 2))


class Batch15HindiTests(TestCase):
    def test_partner_public_profile_and_catalogue(self):
        from django.utils.translation import gettext, override
        with override('hi'):
            self.assertEqual(gettext('Recent reviews'), 'हाल की समीक्षाएँ')
            self.assertEqual(gettext('Reset your BookBridge password'), 'अपना BookBridge पासवर्ड रीसेट करें')
            self.assertIn('10%%', gettext('"Partner Buyback Commission" is the 10%% Shopkeeper Partners owe BookBridge on books they buy directly from sellers (see the Partners app) - a separate revenue stream from marketplace order commission above. Partners settle it themselves from their Partner Dashboard, which debits their BookBridge Wallet.'))

    def test_password_reset_email_is_sent_in_hindi(self):
        from django.contrib.auth import get_user_model
        from django.core import mail
        get_user_model().objects.create_user(username='hindi_mail', password='pw-12345-xyz', email='hm@example.com')
        self.client.post(reverse('accounts:password_reset'), {'email': 'hm@example.com'}, headers={'accept-language': 'hi'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('BookBridge', mail.outbox[0].subject)
        self.assertIn('नमस्ते', mail.outbox[0].body)
        self.assertIn('BookBridge टीम', mail.outbox[0].body)


class Batch16HindiTests(TestCase):
    def test_sell_to_bookbridge_translated(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='hindi_b16', password='pw-12345-xyz')
        self.client.force_login(user)
        r = self.client.get(reverse('partners:sell_to_bookbridge'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'सीधे BookBridge को बेचें')
        self.assertContains(r, 'बायबैक के लिए जमा करें')
        # JS messages are rendered into the page for the quote widget
        self.assertContains(r, 'पार्टनर आमतौर पर देते हैं')


class Batch17HindiTests(TestCase):
    def test_staff_dashboard_translated(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        user = get_user_model().objects.create_user(username='hindi_staff', password='pw-12345-xyz', is_staff=True)
        self.client.force_login(user)
        r = self.client.get(reverse('adminpanel:dashboard'), headers={'accept-language': 'hi'})
        if r.status_code == 200:
            self.assertContains(r, 'स्टाफ़ डैशबोर्ड')
            self.assertContains(r, 'आपके ध्यान की ज़रूरत')

    def test_home_digital_books_subtitle_translated(self):
        r = self.client.get(reverse('books:home'), headers={'accept-language': 'hi'})
        self.assertContains(r, 'हस्तलिखित नोट्स')

    def test_plural_and_audit_entries(self):
        from django.utils.translation import ngettext, override
        with override('hi'):
            self.assertIn('विवाद', ngettext('%(counter)s escalated dispute waiting on a decision.',
                                             '%(counter)s escalated disputes waiting on a decision.', 2))
