"""
Bulk listing upload - lets a seller with more than a handful of books list
many at once via a CSV or Excel (.xlsx) file instead of the one-at-a-time
`add_book` form (see books/views.py:bulk_upload_books).

Deliberately physical-book-only: a digital listing needs an actual file
upload (Book.digital_file) per row, which a spreadsheet can't carry, so
that stays on the regular `books:add` form. Each row goes through the same
AI Fraud Detection pass as a normal listing (run_fraud_scan), just without
image-based checks since bulk rows don't include photos - sellers can add
photos afterwards by editing the listing.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from .fraud_detection import run_fraud_scan
from .models import Book, Category

# Hard ceiling on rows processed per upload - keeps a single request from
# hammering the DB (one INSERT + one fraud scan per valid row) or hanging
# on a mis-sized file someone dragged in by mistake.
MAX_ROWS = 500

TEMPLATE_HEADERS = [
    'title', 'author', 'publisher', 'isbn', 'edition', 'publication_year',
    'category', 'subject', 'board', 'university', 'branch', 'semester', 'language',
    'condition', 'original_price', 'price', 'quantity', 'description', 'is_rentable',
    'pickup_address', 'pickup_city', 'pickup_pincode',
]
REQUIRED_HEADERS = ['title', 'price']

_CONDITION_KEYS = {key for key, _label in Book.CONDITION_CHOICES}
_CONDITION_LABELS = {label.lower(): key for key, label in Book.CONDITION_CHOICES}
_TRUE_VALUES = {'1', 'true', 'yes', 'y'}


class BulkUploadFileError(Exception):
    """The file itself couldn't be read at all (wrong format, empty, corrupt)."""


def _clean_str(value):
    if value is None:
        return ''
    return str(value).strip()


def _to_decimal(value):
    text = _clean_str(value)
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _to_int(value, default=None):
    text = _clean_str(value)
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_bool(value):
    return _clean_str(value).lower() in _TRUE_VALUES


def _normalize_headers(headers):
    return [_clean_str(h).lower() for h in headers]


def read_rows(uploaded_file):
    """Returns a list of {lowercased_header: raw_value} dicts, one per data
    row, for either a .csv or an .xlsx/.xls upload."""
    name = (uploaded_file.name or '').lower()
    if name.endswith('.csv'):
        try:
            text = uploaded_file.read().decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise BulkUploadFileError('That CSV file is not readable text (try saving it as UTF-8 CSV).') from exc
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
    elif name.endswith('.xlsx') or name.endswith('.xls'):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - dependency should always be present
            raise BulkUploadFileError('Excel support is not available on this server - upload a CSV instead.') from exc
        try:
            workbook = openpyxl.load_workbook(uploaded_file, data_only=True, read_only=True)
        except Exception as exc:
            raise BulkUploadFileError(f"Couldn't open that Excel file: {exc}") from exc
        sheet = workbook.active
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    else:
        raise BulkUploadFileError('Please upload a .csv or .xlsx file.')

    if not rows:
        raise BulkUploadFileError('That file has no rows in it.')

    headers = _normalize_headers(rows[0])
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        raise BulkUploadFileError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Download the template to see the expected columns."
        )

    data_rows = []
    for raw_row in rows[1:]:
        if raw_row is None or all(cell in (None, '') for cell in raw_row):
            continue
        row = {}
        for i, header in enumerate(headers):
            row[header] = raw_row[i] if i < len(raw_row) else ''
        data_rows.append(row)
    return data_rows


def process_bulk_upload(uploaded_file, seller):
    """Parses the uploaded file and creates one physical Book listing per
    valid row for `seller`. Returns {'created': [Book, ...],
    'errors': [{'row': int, 'title': str, 'message': str}, ...]}.
    A row that fails validation is skipped (not just truncated) so one bad
    row never blocks the rest of the file."""
    rows = read_rows(uploaded_file)  # raises BulkUploadFileError - let the view handle it

    if len(rows) > MAX_ROWS:
        raise BulkUploadFileError(
            f'That file has {len(rows)} rows - please split it into batches of {MAX_ROWS} or fewer.'
        )

    categories_by_name = {c.name.lower(): c for c in Category.objects.all()}
    created = []
    errors = []

    for idx, row in enumerate(rows, start=2):  # row 1 is the header
        row_errors = []

        title = _clean_str(row.get('title'))
        if not title:
            row_errors.append('Title is required.')

        price = _to_decimal(row.get('price'))
        if price is None:
            row_errors.append('Price is required and must be a number.')
        elif price < 1:
            row_errors.append('Price must be at least \u20b91.')

        original_price = _to_decimal(row.get('original_price'))
        if original_price is not None and original_price < 0:
            row_errors.append('Original price cannot be negative.')

        quantity = _to_int(row.get('quantity'), default=1)
        if quantity is None or quantity < 1:
            row_errors.append('Quantity must be a positive whole number.')
            quantity = quantity or 1

        publication_year = _to_int(row.get('publication_year'), default=None)

        condition_raw = _clean_str(row.get('condition')).lower() or 'good'
        if condition_raw in _CONDITION_KEYS:
            condition = condition_raw
        elif condition_raw in _CONDITION_LABELS:
            condition = _CONDITION_LABELS[condition_raw]
        else:
            row_errors.append(
                f"Unrecognized condition \"{row.get('condition')}\" - use one of: "
                f"{', '.join(k for k, _ in Book.CONDITION_CHOICES)}."
            )
            condition = 'good'

        category = None
        category_name = _clean_str(row.get('category'))
        if category_name:
            category = categories_by_name.get(category_name.lower())
            if category is None:
                row_errors.append(f"Unknown category \"{category_name}\".")

        if row_errors:
            errors.append({'row': idx, 'title': title or '(untitled)', 'message': ' '.join(row_errors)})
            continue

        book = Book(
            seller=seller,
            listing_type='physical',
            category=category,
            title=title,
            author=_clean_str(row.get('author')),
            publisher=_clean_str(row.get('publisher')),
            isbn=_clean_str(row.get('isbn')),
            edition=_clean_str(row.get('edition')),
            publication_year=publication_year,
            subject=_clean_str(row.get('subject')),
            board=_clean_str(row.get('board')),
            university=_clean_str(row.get('university')),
            branch=_clean_str(row.get('branch')),
            semester=_clean_str(row.get('semester')),
            language=_clean_str(row.get('language')) or 'English',
            condition=condition,
            original_price=original_price,
            price=price,
            quantity=quantity,
            description=_clean_str(row.get('description')),
            is_rentable=_to_bool(row.get('is_rentable')),
            pickup_address=_clean_str(row.get('pickup_address')),
            pickup_city=_clean_str(row.get('pickup_city')),
            pickup_pincode=_clean_str(row.get('pickup_pincode')),
        )
        try:
            book.full_clean(exclude=['seller', 'fraud_flags'])
        except ValidationError as exc:
            message = '; '.join(f"{field}: {', '.join(msgs)}" for field, msgs in exc.message_dict.items())
            errors.append({'row': idx, 'title': title, 'message': message})
            continue

        book.save()

        # Same Fraud Detection pass add_book() runs, minus the image-reuse
        # check (bulk rows carry no photos yet).
        fraud_result = run_fraud_scan(
            seller=seller,
            title=book.title,
            description=book.description,
            price=book.price,
            isbn=book.isbn,
            category_id=book.category_id,
            subject=book.subject,
            images=[],
        )
        book.set_fraud_result(fraud_result)
        if fraud_result['hold_for_review']:
            book.status = 'under_review'
        book.save(update_fields=['fraud_score', 'fraud_flags', 'fraud_checked_at', 'status'])

        created.append(book)

    return {'created': created, 'errors': errors}


def build_template_workbook():
    """Builds the downloadable .xlsx template sellers fill in - one example
    row, plus a reference sheet listing the exact category names and
    condition codes this store currently accepts."""
    import openpyxl
    from openpyxl.styles import Font

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = 'Books'
    sheet.append(TEMPLATE_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    category_names = list(Category.objects.order_by('name').values_list('name', flat=True))
    example_category = category_names[0] if category_names else ''
    sheet.append([
        'Introduction to Physics', 'H.C. Verma', 'Bharati Bhawan', '9788177091878', '2nd Edition', 2010,
        example_category, 'Physics', '', '', '', '', 'English',
        'good', 450, 250, 1, 'Lightly used, no torn pages.', 'FALSE',
        '', '', '',
    ])
    for column_cells in sheet.columns:
        length = max(len(str(c.value)) if c.value is not None else 0 for c in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 10), 40)

    reference = workbook.create_sheet('Reference')
    reference.append(['Valid categories', 'Valid condition codes'])
    for cell in reference[1]:
        cell.font = Font(bold=True)
    condition_codes = [f'{key} ({label})' for key, label in Book.CONDITION_CHOICES]
    for i in range(max(len(category_names), len(condition_codes))):
        reference.append([
            category_names[i] if i < len(category_names) else '',
            condition_codes[i] if i < len(condition_codes) else '',
        ])
    reference.column_dimensions['A'].width = 30
    reference.column_dimensions['B'].width = 30

    return workbook
