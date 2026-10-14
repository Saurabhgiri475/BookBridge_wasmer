"""
Shared sustainability-impact math for BookBridge.

Every number here starts from a single real figure - a count of books that
changed hands via a completed (delivered) order - and multiplies it by a
documented, editable coefficient. These are estimates for branding/marketing
and grant applications, not exact measurements. Tune the constants below if
you have better data for your own supply chain; every dashboard (community,
personal, and per-user badges) reads from this one place so they always
stay consistent with each other.
"""

BOOKS_PER_TREE = 130            # ~130 books can be produced from one tree
KG_PAPER_PER_BOOK = 0.5         # average paper weight per book, in kg
LITRES_WATER_PER_BOOK = 150     # litres of water used to manufacture one new book
KG_CO2_PER_BOOK = 0.5           # kg of CO2 emitted producing one new book
KWH_ENERGY_PER_BOOK = 0.8       # kWh of energy used producing one new book

COEFFICIENTS = {
    'books_per_tree': BOOKS_PER_TREE,
    'kg_paper_per_book': KG_PAPER_PER_BOOK,
    'litres_water_per_book': LITRES_WATER_PER_BOOK,
    'kg_co2_per_book': KG_CO2_PER_BOOK,
    'kwh_energy_per_book': KWH_ENERGY_PER_BOOK,
}

# Badge thresholds, in ascending order. "books_reused" for a user is
# purchases + sales that both completed (delivered) - i.e. every time a
# secondhand book moved through them instead of a new one being printed.
BADGES = [
    {'code': 'eco_starter', 'name': 'Eco Starter', 'emoji': '🥉', 'threshold': 5,
     'description': 'Reuse 5 books'},
    {'code': 'green_reader', 'name': 'Green Reader', 'emoji': '🥈', 'threshold': 20,
     'description': 'Reuse 20 books'},
    {'code': 'book_hero', 'name': 'Book Hero', 'emoji': '🥇', 'threshold': 50,
     'description': 'Reuse 50 books'},
    {'code': 'planet_protector', 'name': 'Planet Protector', 'emoji': '🌍', 'threshold': 100,
     'description': 'Reuse 100 books'},
]


def compute_impact(books_reused):
    """Given a count of reused books, return the full set of estimated impact figures."""
    books_reused = books_reused or 0
    trees_saved = books_reused / BOOKS_PER_TREE
    paper_saved_kg = books_reused * KG_PAPER_PER_BOOK
    water_saved_litres = books_reused * LITRES_WATER_PER_BOOK
    co2_saved_kg = books_reused * KG_CO2_PER_BOOK
    energy_saved_kwh = books_reused * KWH_ENERGY_PER_BOOK

    return {
        'books_reused': books_reused,
        'trees_saved': trees_saved,
        'paper_saved_kg': paper_saved_kg,
        'water_saved_litres': water_saved_litres,
        'co2_saved_kg': co2_saved_kg,
        'co2_saved_tons': co2_saved_kg / 1000,
        'energy_saved_kwh': energy_saved_kwh,
    }


def badges_earned(books_reused):
    """Badges a user has already unlocked, highest first."""
    books_reused = books_reused or 0
    return [b for b in reversed(BADGES) if books_reused >= b['threshold']]


def next_badge(books_reused):
    """The next badge still to unlock, plus how many more books are needed - or None if all earned."""
    books_reused = books_reused or 0
    for b in BADGES:
        if books_reused < b['threshold']:
            return {**b, 'books_to_go': b['threshold'] - books_reused}
    return None
