"""
Tests for books/ml_recommendations.py (the trained scikit-learn model) and
books/recommendations.also_bought() (which tries that model first and falls
back to a real-time SQL query).

Uses a temp file for MODEL_PATH throughout, via override_model_path(), so
these tests never touch a real ml_models/ directory or interfere with each
other.
"""
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from accounts.models import User
from orders.models import Order

from . import ml_recommendations, recommendations
from .ml_recommendations import NotEnoughData, build_model, load_model, save_model, similar_book_ids
from .models import Book, Category
from .recommendations import also_bought


def make_book(seller, **kwargs):
    category, _ = Category.objects.get_or_create(name='Test Category')
    defaults = {'title': 'Some Book', 'price': 199, 'status': 'available'}
    defaults.update(kwargs)
    return Book.objects.create(seller=seller, category=category, **defaults)


def make_delivered_order(buyer, book):
    return Order.objects.create(
        buyer=buyer, book=book, final_price=Decimal(book.price), delivery_charge=Decimal('40'),
        payment_method='cod', status='delivered',
        delivery_address='x', delivery_city='y', delivery_pincode='123456',
    )


@contextmanager
def override_model_path():
    """Points ml_recommendations.MODEL_PATH at a throwaway temp file for the
    duration of the block, so tests never write to a real ml_models/ dir."""
    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / 'item_similarity.joblib'
        with patch.object(ml_recommendations, 'MODEL_PATH', fake_path):
            yield fake_path


class BuildModelTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='ml_seller', password='pw')

    def test_raises_not_enough_data_when_thin(self):
        buyer = User.objects.create_user(username='ml_buyer1', password='pw')
        book = make_book(self.seller, title='Only Book')
        make_delivered_order(buyer, book)  # a single buyer, single purchase - nowhere near enough

        with self.assertRaises(NotEnoughData):
            build_model()

    def test_builds_similarity_between_co_bought_books(self):
        # Five buyers, each buying the same pair of books - a clean signal
        # that Book A and Book B are "similar" (bought by the same people),
        # while Book C (bought only by a single unrelated buyer) is not.
        book_a = make_book(self.seller, title='Book A')
        book_b = make_book(self.seller, title='Book B')
        book_c = make_book(self.seller, title='Book C')

        for i in range(5):
            buyer = User.objects.create_user(username=f'coshopper{i}', password='pw')
            make_delivered_order(buyer, book_a)
            make_delivered_order(buyer, book_b)

        lone_buyer = User.objects.create_user(username='lone_buyer', password='pw')
        make_delivered_order(lone_buyer, book_c)

        book_ids, similarity_matrix = build_model()

        a_idx, b_idx, c_idx = book_ids.index(book_a.pk), book_ids.index(book_b.pk), book_ids.index(book_c.pk)
        self.assertGreater(similarity_matrix[a_idx][b_idx], 0.9)
        self.assertEqual(similarity_matrix[a_idx][c_idx], 0)
        # A book is never listed as similar to itself.
        self.assertEqual(similarity_matrix[a_idx][a_idx], 0)


class SaveLoadModelTests(TestCase):
    def test_round_trips_through_disk(self):
        with override_model_path():
            book_ids = [1, 2, 3]
            import numpy as np
            similarity_matrix = np.array([[0, 0.8, 0.1], [0.8, 0, 0.2], [0.1, 0.2, 0]])

            save_model(book_ids, similarity_matrix)
            loaded = load_model()

            self.assertIsNotNone(loaded)
            loaded_ids, loaded_matrix = loaded
            self.assertEqual(loaded_ids, book_ids)
            self.assertEqual(loaded_matrix[0][1], 0.8)

    def test_missing_file_returns_none(self):
        with override_model_path():
            self.assertIsNone(load_model())

    def test_corrupt_file_returns_none_instead_of_raising(self):
        with override_model_path() as path:
            path.write_bytes(b'not a valid joblib file')
            self.assertIsNone(load_model())


class SimilarBookIdsTests(TestCase):
    def test_ranks_by_similarity_descending(self):
        import numpy as np
        book_ids = [10, 20, 30, 40]
        similarity_matrix = np.array([
            [0, 0.9, 0.5, 0.1],
            [0.9, 0, 0.2, 0.3],
            [0.5, 0.2, 0, 0.0],
            [0.1, 0.3, 0.0, 0],
        ])

        result = similar_book_ids(10, book_ids, similarity_matrix, limit=3)

        self.assertEqual(result, [20, 30, 40])

    def test_zero_similarity_is_excluded(self):
        import numpy as np
        book_ids = [10, 20, 30]
        similarity_matrix = np.array([[0, 0.9, 0], [0.9, 0, 0], [0, 0, 0]])

        result = similar_book_ids(10, book_ids, similarity_matrix, limit=5)

        self.assertEqual(result, [20])

    def test_unknown_book_returns_empty(self):
        import numpy as np
        result = similar_book_ids(999, [1, 2, 3], np.zeros((3, 3)), limit=5)
        self.assertEqual(result, [])


class AlsoBoughtFallbackTests(TestCase):
    """also_bought() must use a trained model when one covers the book, and
    fall back to the real-time SQL query otherwise - never return nothing
    just because training hasn't run yet or missed a recent book."""

    def setUp(self):
        self.seller = User.objects.create_user(username='fallback_seller', password='pw')
        self.book_a = make_book(self.seller, title='Fallback Book A')
        self.book_b = make_book(self.seller, title='Fallback Book B')
        buyer = User.objects.create_user(username='fallback_buyer', password='pw')
        make_delivered_order(buyer, self.book_a)
        make_delivered_order(buyer, self.book_b)

    def test_no_trained_model_uses_sql_fallback(self):
        with override_model_path():
            result = also_bought(self.book_a, limit=6)

        self.assertIn(self.book_b, result)

    def test_book_not_covered_by_model_uses_sql_fallback(self):
        import numpy as np
        with override_model_path():
            # A model that exists but knows nothing about book_a/book_b.
            save_model([999999], np.zeros((1, 1)))
            result = also_bought(self.book_a, limit=6)

        self.assertIn(self.book_b, result)

    def test_trained_model_result_is_used_when_available(self):
        import numpy as np
        with override_model_path():
            book_ids = [self.book_a.pk, self.book_b.pk]
            similarity_matrix = np.array([[0, 0.99], [0.99, 0]])
            save_model(book_ids, similarity_matrix)

            result = also_bought(self.book_a, limit=6)

        self.assertEqual(result, [self.book_b])

    def test_sold_out_recommendation_is_excluded(self):
        import numpy as np
        with override_model_path():
            self.book_b.status = 'sold'
            self.book_b.save(update_fields=['status'])
            book_ids = [self.book_a.pk, self.book_b.pk]
            similarity_matrix = np.array([[0, 0.99], [0.99, 0]])
            save_model(book_ids, similarity_matrix)

            result = also_bought(self.book_a, limit=6)

        self.assertEqual(result, [])
