from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from chatapp.models import Conversation, Message
from notifications.models import Notification
from partners.models import BuybackRequest
from partners.tests import _make_partner


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class BuybackChatTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('sel', 'sel@x.com', 'pw12345!')
        self.shop_user = User.objects.create_user('shop', 'shop@x.com', 'pw12345!')
        self.shop2_user = User.objects.create_user('shop2', 'shop2@x.com', 'pw12345!')
        self.rando = User.objects.create_user('rando', 'r@x.com', 'pw12345!')
        self.partner = _make_partner(self.shop_user, status='approved')
        self.partner2 = _make_partner(self.shop2_user, status='approved')
        self.lead = BuybackRequest.objects.create(
            seller=self.seller, title='Chem Vol 2', pickup_address='a', pickup_city='Lucknow',
        )

    def _start(self, username, pk=None):
        self.client.login(username=username, password='pw12345!')
        return self.client.get(reverse('chatapp:start_buyback', args=[pk or self.lead.pk]))

    def test_partner_can_open_chat_on_open_lead(self):
        resp = self._start('shop')
        convo = Conversation.objects.get()
        self.assertRedirects(resp, reverse('chatapp:conversation', args=[convo.pk]))
        self.assertEqual((convo.buyback, convo.buyer, convo.seller, convo.book), (self.lead, self.shop_user, self.seller, None))

    def test_starting_twice_reuses_thread_and_two_partners_get_separate_threads(self):
        self._start('shop')
        self._start('shop')
        self._start('shop2')
        self.assertEqual(Conversation.objects.count(), 2)

    def test_seller_cannot_chat_until_partner_attached_then_can(self):
        resp = self._start('sel')
        self.assertRedirects(resp, reverse('partners:buyback_detail', args=[self.lead.pk]))
        self.assertEqual(Conversation.objects.count(), 0)

        self.lead.partner, self.lead.status = self.partner, 'offer_made'
        self.lead.save()
        self._start('sel')
        convo = Conversation.objects.get()
        self.assertEqual(convo.buyer, self.shop_user)

    def test_partner_cannot_chat_on_other_partners_lead_or_cancelled_lead(self):
        self.lead.partner, self.lead.status = self.partner, 'offer_made'
        self.lead.save()
        self.assertEqual(self._start('shop2').status_code, 404)
        self.lead.status = 'cancelled'
        self.lead.save()
        self.assertEqual(self._start('shop').status_code, 404)

    def test_non_partner_stranger_gets_404(self):
        self.assertEqual(self._start('rando').status_code, 404)

    def test_partners_own_request_never_creates_self_chat(self):
        mine = BuybackRequest.objects.create(
            seller=self.shop_user, title='Mine', pickup_address='a', pickup_city='Lucknow',
        )
        resp = self._start('shop', pk=mine.pk)  # they're the seller here, with no partner attached yet
        self.assertRedirects(resp, reverse('partners:buyback_detail', args=[mine.pk]))
        self.assertEqual(Conversation.objects.count(), 0)

    def test_messaging_notifies_other_side_and_outsiders_blocked(self):
        self._start('shop')
        convo = Conversation.objects.get()
        url = reverse('chatapp:conversation', args=[convo.pk])
        self.client.post(url, {'text': 'Can I collect Sunday?'})
        self.assertEqual(Message.objects.count(), 1)
        n = Notification.objects.get(user=self.seller)
        self.assertIn('Chem Vol 2', n.body)

        self.client.login(username='sel', password='pw12345!')
        self.assertContains(self.client.get(url), 'Can I collect Sunday?')
        self.assertContains(self.client.get(reverse('chatapp:inbox')), 'Chem Vol 2')

        self.client.login(username='rando', password='pw12345!')
        self.assertRedirects(self.client.get(url), reverse('chatapp:inbox'))

    def test_book_conversations_still_work(self):
        from books.models import Book
        book = Book.objects.create(seller=self.seller, title='Maths', price=100, pickup_city='x', pickup_address='y', status='available')
        self.client.login(username='rando', password='pw12345!')
        self.client.get(reverse('chatapp:start', args=[book.pk]))
        convo = Conversation.objects.get(book=book)
        self.assertFalse(convo.is_buyback)
        self.assertEqual(convo.subject_title, 'Maths')
        self.assertContains(self.client.get(reverse('chatapp:inbox')), 'Maths')
