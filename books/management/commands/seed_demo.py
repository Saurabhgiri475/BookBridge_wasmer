"""
python manage.py seed_demo

Generates the training dataset for books/ml_recommendations.py's item-item
collaborative filtering model. That module's NotEnoughData message already
points here ("...or run `seed_demo` for local testing") - this is that
command.

There's no CSV involved: the "dataset" this recommender learns from is
completed Order history (buyer_id, book_id) sitting in the database (see
build_model() in books/ml_recommendations.py), so "generate a dataset"
means populating the DB with realistic Users, Books and Orders rather than
writing a file to disk.

What it creates
----------------
A handful of subject "clusters" (JEE Physics, NEET Biology, B.Tech CSE
Sem 3, ...), each with several book listings from different demo sellers.
Then a batch of demo buyers, each with an interest in one or two clusters,
who each "buy" (via a delivered Order) several books from their cluster(s).
Buying mostly-within-cluster is what gives the cosine-similarity model in
ml_recommendations.py real signal: books repeatedly bought by the same
people end up scored as similar, which is the entire point of item-item CF.

Idempotent: re-running clears out any previously-seeded demo rows first
(everything tagged with the 'demo_' username/prefix below), so you can run
this again after tweaking cluster sizes without accumulating duplicates.
Nothing outside that prefix is touched - your real users/books/orders are
never read or modified.

After running this, train the model with:
    python manage.py train_recommendations
"""
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

DEMO_PREFIX = 'demo_'

# Each cluster is a subject/course grouping - the whole point is that these
# books plausibly get bought together by the same kind of buyer, which is
# the signal item-item CF is trying to learn.
CLUSTERS = [
    {
        'name': 'JEE Physics',
        'group': 'competitive',
        'meta': {'subject': 'Physics', 'board': 'JEE'},
        'books': [
            ('NCERT Physics Class 11', 'NCERT'),
            ('NCERT Physics Class 12', 'NCERT'),
            ('Concepts of Physics Vol 1', 'H.C. Verma'),
            ('Concepts of Physics Vol 2', 'H.C. Verma'),
            ('Understanding Physics - Mechanics', 'D.C. Pandey'),
            ('Problems in General Physics', 'I.E. Irodov'),
        ],
    },
    {
        'name': 'JEE Chemistry',
        'group': 'competitive',
        'meta': {'subject': 'Chemistry', 'board': 'JEE'},
        'books': [
            ('NCERT Chemistry Class 11', 'NCERT'),
            ('NCERT Chemistry Class 12', 'NCERT'),
            ('Physical Chemistry', 'O.P. Tandon'),
            ('Organic Chemistry Problems', 'M.S. Chouhan'),
            ('Concise Inorganic Chemistry', 'J.D. Lee'),
        ],
    },
    {
        'name': 'NEET Biology',
        'group': 'competitive',
        'meta': {'subject': 'Biology', 'board': 'NEET'},
        'books': [
            ('NCERT Biology Class 11', 'NCERT'),
            ('NCERT Biology Class 12', 'NCERT'),
            ("Trueman's Biology Vol 1", 'K.N. Bhatia'),
            ("Trueman's Biology Vol 2", 'K.N. Bhatia'),
            ('Objective Biology for NEET', 'GRB'),
        ],
    },
    {
        'name': 'CBSE Class 12 Boards',
        'group': 'academic',
        'meta': {'subject': 'Mixed', 'board': 'CBSE'},
        'books': [
            ('Mathematics Class 12', 'R.D. Sharma'),
            ('Physics NCERT Exemplar Class 12', 'NCERT'),
            ('Flamingo - English Core Class 12', 'NCERT'),
            ('Accountancy Class 12', 'D.K. Goel'),
            ('Economics Class 12', 'NCERT'),
        ],
    },
    {
        'name': 'B.Tech CSE Semester 3',
        'group': 'college',
        'meta': {'university': 'Anna University', 'branch': 'CSE', 'semester': '3'},
        'books': [
            ('Data Structures', 'Seymour Lipschutz'),
            ('Digital Logic and Computer Design', 'M. Morris Mano'),
            ('Discrete Mathematics and Its Applications', 'Kenneth Rosen'),
            ('Computer Organization', 'Carl Hamacher'),
            ('Design and Analysis of Algorithms', 'Ellis Horowitz'),
        ],
    },
    {
        'name': 'B.Tech Mechanical Semester 4',
        'group': 'college',
        'meta': {'university': 'Anna University', 'branch': 'Mechanical', 'semester': '4'},
        'books': [
            ('Engineering Thermodynamics', 'P.K. Nag'),
            ('Fluid Mechanics and Hydraulic Machines', 'R.K. Bansal'),
            ('Strength of Materials', 'R.K. Rajput'),
            ('Theory of Machines', 'R.S. Khurmi'),
        ],
    },
    {
        'name': 'UPSC General Studies',
        'group': 'competitive',
        'meta': {'subject': 'General Studies', 'board': 'UPSC'},
        'books': [
            ('Indian Polity', 'M. Laxmikanth'),
            ('India Since Independence', 'Bipan Chandra'),
            ('Certificate Physical and Human Geography', 'G.C. Leong'),
            ('Indian Economy', 'Ramesh Singh'),
        ],
    },
    {
        'name': 'CA Foundation',
        'group': 'others',
        'meta': {'subject': 'Commerce', 'board': 'ICAI'},
        'books': [
            ('Principles and Practice of Accounting', 'ICAI Study Material'),
            ('Business Law', 'Munish Bhandari'),
            ('Business Mathematics and Statistics', 'ICAI Study Material'),
        ],
    },
]

CONDITIONS = ['like_new', 'excellent', 'good', 'good', 'fair']  # weighted toward "good"
CITIES = ['Mumbai', 'Delhi', 'Bengaluru', 'Chennai', 'Pune', 'Hyderabad', 'Lucknow', 'Kolkata']

NUM_SELLERS = 10
NUM_BUYERS = 60
# Each buyer buys this many books from their primary cluster, plus
# optionally a couple from a secondary one - see handle() below.
PRIMARY_PURCHASES = (2, 4)
SECONDARY_PURCHASES = (0, 2)
SECONDARY_CHANCE = 0.4  # fraction of buyers who also dabble in a second cluster


class Command(BaseCommand):
    help = (
        'Seeds demo Users/Books/Orders with realistic co-purchase clusters, '
        'so books.ml_recommendations has real purchase history to train the '
        'item-item collaborative filtering model from. Run '
        '`python manage.py train_recommendations` afterwards.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--seed', type=int, default=42,
            help='Random seed, for reproducible runs (default: 42).',
        )

    def handle(self, *args, **options):
        random.seed(options['seed'])

        from accounts.models import User
        from books.models import Book, Category
        from orders.models import Order

        with transaction.atomic():
            self._flush_previous_run(User)

            sellers = self._create_sellers(User)
            buyers = self._create_buyers(User)
            books_by_cluster = self._create_books(Book, Category, sellers)
            orders_created = self._create_orders(Order, buyers, books_by_cluster)

        total_books = sum(len(v) for v in books_by_cluster.values())
        repeat_buyers = sum(1 for _, books in orders_created.items() if len(books) >= 2)

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(sellers)} sellers, {len(buyers)} buyers, {total_books} books '
            f'across {len(CLUSTERS)} clusters, and {sum(len(b) for b in orders_created.values())} '
            f'orders ({repeat_buyers} buyers with 2+ purchases).'
        ))
        self.stdout.write('Now run: python manage.py train_recommendations')

    def _flush_previous_run(self, User):
        """Delete demo rows from a previous run so re-running this command
        doesn't pile up duplicate users/books/orders. Cascades to that
        user's Books and Orders via on_delete=CASCADE, so deleting the demo
        Users is enough - nothing outside the demo_ prefix is touched."""
        deleted, _ = User.objects.filter(username__startswith=DEMO_PREFIX).delete()
        if deleted:
            self.stdout.write(f'Cleared {deleted} row(s) from a previous seed_demo run.')

    def _create_sellers(self, User):
        sellers = []
        for i in range(1, NUM_SELLERS + 1):
            seller = User.objects.create_user(
                username=f'{DEMO_PREFIX}seller_{i}',
                email=f'{DEMO_PREFIX}seller_{i}@example.com',
                password='demopass123',
                role='seller',
                city=random.choice(CITIES),
            )
            sellers.append(seller)
        return sellers

    def _create_buyers(self, User):
        buyers = []
        for i in range(1, NUM_BUYERS + 1):
            buyer = User.objects.create_user(
                username=f'{DEMO_PREFIX}buyer_{i}',
                email=f'{DEMO_PREFIX}buyer_{i}@example.com',
                password='demopass123',
                role='buyer',
                city=random.choice(CITIES),
            )
            buyers.append(buyer)
        return buyers

    def _create_books(self, Book, Category, sellers):
        """Returns {cluster_name: [Book, ...]}. Each book is assigned to a
        random demo seller and left status='available' (synthetic Orders
        below represent *historical* purchases without depleting stock, so
        recommendations.py's status='available' filter still surfaces
        them)."""
        books_by_cluster = {}
        for cluster in CLUSTERS:
            category, _ = Category.objects.get_or_create(
                name=cluster['name'], defaults={'group': cluster['group']},
            )
            cluster_books = []
            for title, author in cluster['books']:
                price = Decimal(random.choice([150, 180, 220, 250, 300, 350, 400]))
                book = Book.objects.create(
                    seller=random.choice(sellers),
                    category=category,
                    listing_type='physical',
                    title=title,
                    author=author,
                    condition=random.choice(CONDITIONS),
                    price=price,
                    original_price=(price * Decimal('1.4')).quantize(Decimal('1')),
                    quantity=random.randint(1, 3),
                    pickup_city=random.choice(CITIES),
                    status='available',
                    **cluster['meta'],
                )
                cluster_books.append(book)
            books_by_cluster[cluster['name']] = cluster_books
        return books_by_cluster

    def _create_orders(self, Order, buyers, books_by_cluster):
        """Returns {buyer_id: [book_id, ...]} for the summary line in
        handle(). Every buyer gets a primary cluster; ~40% also dabble in a
        second one, so the interaction matrix isn't perfectly block-diagonal
        (real buyers aren't either) while still keeping a strong
        within-cluster signal for cosine similarity to pick up on."""
        cluster_names = list(books_by_cluster.keys())
        orders_by_buyer = {}

        for buyer in buyers:
            primary, secondary = random.sample(cluster_names, 2)
            purchased_books = []

            primary_books = books_by_cluster[primary]
            n_primary = min(random.randint(*PRIMARY_PURCHASES), len(primary_books))
            purchased_books += random.sample(primary_books, n_primary)

            if random.random() < SECONDARY_CHANCE:
                secondary_books = books_by_cluster[secondary]
                n_secondary = min(random.randint(*SECONDARY_PURCHASES), len(secondary_books))
                purchased_books += random.sample(secondary_books, n_secondary)

            for book in purchased_books:
                Order.objects.create(
                    buyer=buyer,
                    book=book,
                    order_type='purchase',
                    status='delivered',
                    final_price=book.price,
                    delivery_charge=Decimal('0') if book.is_digital else Decimal('40'),
                    payment_method='cod',
                )
            orders_by_buyer[buyer.id] = [b.id for b in purchased_books]

        return orders_by_buyer
