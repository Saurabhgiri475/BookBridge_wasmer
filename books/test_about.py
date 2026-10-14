from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from books.models import Book, Category
from disputes import services as dispute_policy
from orders.models import Order


class AboutPageTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user('sel', 's@x.com', 'pw')
        self.buyer = User.objects.create_user('buy', 'b@x.com', 'pw')
        self.cat = Category.objects.create(name='Class 10 CBSE', group='academic')
        self.empty_cat = Category.objects.create(name='Nothing Here', group='others')

    def get(self):
        resp = self.client.get(reverse('books:about'))
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_renders_with_images_and_alt_text(self):
        html = self.get().content.decode()
        for name in ('hero.svg', 'mission.svg'):
            self.assertIn(f'img/about/{name}', html)
        self.assertEqual(html.count('<img'), html.count(' alt="'))

    def test_illustrations_exist_and_are_valid_svg(self):
        from pathlib import Path
        from xml.dom import minidom
        root = Path(__file__).resolve().parent.parent / 'static' / 'img' / 'about'
        for name in ('hero.svg', 'mission.svg'):
            minidom.parse(str(root / name))

    def test_stats_are_real_counts(self):
        Book.objects.create(seller=self.seller, category=self.cat, title='A', price=100)
        Book.objects.create(seller=self.seller, category=self.cat, title='B', price=100, status='sold')
        ctx = self.get().context
        self.assertEqual(ctx['books_listed'], 1)  # sold books don't count
        self.assertEqual(ctx['total_users'], 2)
        self.assertEqual(ctx['orders_completed'], 0)
        self.assertEqual(ctx['partner_shops'], 0)

    def test_only_categories_with_available_books_are_listed(self):
        Book.objects.create(seller=self.seller, category=self.cat, title='A', price=100)
        Book.objects.create(seller=self.seller, category=self.empty_cat, title='Gone', price=100, status='sold')
        resp = self.get()
        self.assertContains(resp, 'Class 10 CBSE')
        self.assertNotContains(resp, 'Nothing Here')
        self.assertContains(resp, f'?category={self.cat.pk}')

    def test_impact_empty_state_then_real_numbers(self):
        self.assertContains(self.get(), 'The counter starts with the first delivered order')
        book = Book.objects.create(seller=self.seller, category=self.cat, title='A', price=100, status='sold')
        Order.objects.create(buyer=self.buyer, book=book, final_price=100, status='delivered')
        resp = self.get()
        self.assertNotContains(resp, 'The counter starts')
        self.assertEqual(resp.context['impact']['books_reused'], 1)
        self.assertContains(resp, 'books reused')

    def test_faq_quotes_the_real_dispute_policy(self):
        html = self.get().content.decode()
        self.assertIn(f'{dispute_policy.NOT_RECEIVED_AFTER_DAYS} days after placing the order', html)
        self.assertIn(f'within {dispute_policy.DISPUTE_WINDOW_DAYS} days of delivery', html)
        self.assertIn(f'{dispute_policy.SELLER_RESPONSE_DAYS} days to reply', html)

    def test_support_contact_and_links_work(self):
        html = self.get().content.decode()
        self.assertIn('tel:+917985486802', html)
        self.assertIn('mailto:saurabhgiri95893@gmail.com', html)
        for name in ('books:list', 'books:contact', 'books:sustainability', 'donations:home', 'accounts:password_reset'):
            self.assertIn(reverse(name), html, name)

    def test_cta_depends_on_login(self):
        self.assertContains(self.get(), reverse('accounts:signup'))
        self.assertNotContains(self.get(), reverse('books:add'))
        self.client.login(username='buy', password='pw')
        resp = self.get()
        self.assertContains(resp, reverse('books:add'))
        self.assertNotContains(resp, 'Join free')

    def test_faq_accordion_targets_exist(self):
        import re
        html = self.get().content.decode()
        targets = re.findall(r'data-bs-target="#(faq\d+)"', html)
        self.assertEqual(len(targets), 6)
        for t in targets:
            self.assertIn(f'id="{t}"', html)
