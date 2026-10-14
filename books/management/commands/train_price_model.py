"""
python manage.py train_price_model

Trains the ML price model in books/ml_price_model.py from current completed
sales (Order history) and saves it to books/ml_price_model.py's MODEL_PATH.
Run this periodically (a cron job, a scheduled task, or by hand after a
batch of sales) - it's a batch job, not something that runs on every page
load, mirroring train_recommendations.py.

If there isn't enough sales history yet, this reports that plainly and
leaves any previously-trained model in place rather than overwriting it
with a model trained on too little data - price_recommendation.py falls
back to its rule-based blend whenever no trained model is available or
loadable, so nothing breaks in the meantime.
"""
from django.core.management.base import BaseCommand

from books.ml_price_model import MIN_TRAINING_SAMPLES, MODEL_PATH, NotEnoughData, build_model, save_model


class Command(BaseCommand):
    help = 'Train the ML price prediction model from completed sales (Order history).'

    def handle(self, *args, **options):
        try:
            model, sample_count = build_model()
        except NotEnoughData as exc:
            self.stdout.write(self.style.WARNING(str(exc)))
            self.stdout.write(self.style.WARNING(
                f'Need {MIN_TRAINING_SAMPLES}+ completed sales - '
                f'leaving any existing model at {MODEL_PATH} untouched.'
            ))
            return

        save_model(model, sample_count)
        self.stdout.write(self.style.SUCCESS(
            f'Trained price model on {sample_count} completed sales - saved to {MODEL_PATH}.'
        ))
