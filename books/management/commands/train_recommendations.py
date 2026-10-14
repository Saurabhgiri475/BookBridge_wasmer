"""
python manage.py train_recommendations

Trains the item-item collaborative filtering model in
books/ml_recommendations.py from current Order history and saves it to
books/ml_recommendations.py's MODEL_PATH. Run this periodically (a cron job,
a scheduled task, or by hand after a batch of orders) - it's a batch
job, not something that runs on every page load, since real order history
doesn't change fast enough to need retraining on every request and doing
the matrix math per-request would be wasteful.

If there isn't enough purchase history yet, this reports that plainly and
leaves any previously-trained model in place rather than overwriting it
with a model trained on too little data - books/recommendations.py falls
back to its real-time SQL query whenever no trained model is available or
loadable, so nothing breaks in the meantime.
"""
from django.core.management.base import BaseCommand

from books.ml_recommendations import (
    MIN_BUYERS_WITH_REPEAT_PURCHASES, MODEL_PATH, NotEnoughData, build_model, save_model,
)


class Command(BaseCommand):
    help = 'Train the item-item collaborative filtering recommendation model from Order history.'

    def handle(self, *args, **options):
        try:
            book_ids, similarity_matrix = build_model()
        except NotEnoughData as exc:
            self.stdout.write(self.style.WARNING(str(exc)))
            self.stdout.write(self.style.WARNING(
                f'Need {MIN_BUYERS_WITH_REPEAT_PURCHASES}+ buyers with repeat purchases - '
                f'leaving any existing model at {MODEL_PATH} untouched.'
            ))
            return

        save_model(book_ids, similarity_matrix)
        self.stdout.write(self.style.SUCCESS(
            f'Trained recommendation model on {len(book_ids)} books - saved to {MODEL_PATH}.'
        ))
