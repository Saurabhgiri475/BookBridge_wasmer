from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from bookmarket.security import safe_next_url, rate_limit
from django.urls import reverse
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.formats import date_format

from .models import Book, Category, Wishlist, RecentlyViewed, BookRequest, SavedSearch, ListingReport
from .forms import BookForm, BookImageFormSet, BookRequestForm, ListingReportForm, BulkUploadForm
from .bulk_import import process_bulk_upload, build_template_workbook, BulkUploadFileError, TEMPLATE_HEADERS
from .search_ai import parse_search_query
from .matching import find_matching_saved_searches, find_matching_book_requests, title_overlap
from .ocr_utils import scan_cover_image
from .isbn_lookup import lookup_isbn
from .price_recommendation import recommend_price
from .duplicate_check import find_duplicate_listings
from .fraud_detection import run_fraud_scan
from .recommendations import also_bought
from bookmarket.sustainability import compute_impact, COEFFICIENTS
from donations.services import books_donated_total, donation_stats
from bookmarket.notify import notify_saved_search_match, notify_request_fulfilled, notify_price_drop
from orders.models import Offer, Order
from wallet.services import get_wallet, debit as wallet_debit, InsufficientBalance


def home(request):
    featured = Book.objects.filter(status='available').filter(
        Q(is_featured=True) | Q(featured_until__gte=timezone.now())
    ).order_by('-featured_until')[:8]
    recent = Book.objects.filter(status='available', listing_type='physical')[:12]
    digital_recent = Book.objects.filter(status='available', listing_type='digital')[:8]
    categories = Category.objects.all()

    recommended = []
    if request.user.is_authenticated:
        viewed_categories = RecentlyViewed.objects.filter(user=request.user).values_list(
            'book__category', flat=True
        )
        if viewed_categories:
            recommended = Book.objects.filter(
                category__in=viewed_categories, status='available'
            ).exclude(seller=request.user).distinct()[:8]

    context = {
        'featured': featured,
        'recent': recent,
        'digital_recent': digital_recent,
        'categories': categories,
        'recommended': recommended,
        'community_impact': compute_impact(Order.objects.filter(status='delivered').count() + books_donated_total()),
        'digital_type_choices': Book.DIGITAL_TYPE_CHOICES,
    }
    return render(request, 'books/home.html', context)


def contact(request):
    """Help / contact page. The phone and email come from settings (SUPPORT_PHONE / SUPPORT_EMAIL)."""
    return render(request, 'books/contact.html')


def about(request):
    """
    Public "About BookBridge" page. Every number on it is a real count from the
    database (or the same estimate maths as the sustainability dashboard) - nothing
    is made up for marketing. The policy numbers quoted in the FAQ are read from the
    code that enforces them, so the page can't drift out of date.
    """
    from django.db.models import Count
    from accounts.models import User
    from disputes import services as dispute_policy
    from partners.models import ShopkeeperPartner

    books_sold = Order.objects.filter(status='delivered').count()
    giving = donation_stats()
    impact = compute_impact(books_sold + giving['books_donated'])
    categories = (
        Category.objects.annotate(n=Count('books', filter=Q(books__status='available')))
        .filter(n__gt=0).order_by('-n', 'name')[:12]
    )
    context = {
        'books_listed': Book.objects.filter(status='available').count(),
        'total_users': User.objects.filter(is_active=True).count(),
        'orders_completed': books_sold,
        'partner_shops': ShopkeeperPartner.objects.filter(status='approved').count(),
        'categories': categories,
        'impact': impact,
        'books_donated': giving['books_donated'],
        'not_received_days': dispute_policy.NOT_RECEIVED_AFTER_DAYS,
        'dispute_window_days': dispute_policy.DISPUTE_WINDOW_DAYS,
        'seller_reply_days': dispute_policy.SELLER_RESPONSE_DAYS,
    }
    return render(request, 'books/about.html', context)


def sustainability_dashboard(request):
    """
    Public-facing environmental impact dashboard - useful for branding and
    grant applications. All numbers are estimates derived from one real
    figure (books reused = completed orders + books donated and verified
    received, see donations/) multiplied by documented, editable
    coefficients - see bookmarket/sustainability.py.
    """
    books_sold = Order.objects.filter(status='delivered').count()
    giving = donation_stats()
    books_reused = books_sold + giving['books_donated']
    impact = compute_impact(books_reused)
    context = {
        **impact, 'coefficients': COEFFICIENTS,
        'books_sold': books_sold, 'giving': giving,
    }
    return render(request, 'books/sustainability.html', context)


def book_list(request):
    q = request.GET.get('q', '').strip()
    category_id = request.GET.get('category', '')
    board = request.GET.get('board', '')
    university = request.GET.get('university', '')
    semester = request.GET.get('semester', '')
    condition = request.GET.get('condition', '')
    city = request.GET.get('city', '')
    min_price = request.GET.get('min_price', '')
    max_price = request.GET.get('max_price', '')
    listing_type = request.GET.get('listing_type', '')
    digital_type = request.GET.get('digital_type', '')
    ai_mode = request.GET.get('ai', '')

    books = Book.objects.filter(status='available').select_related('seller', 'category')
    parsed = None

    if q:
        if ai_mode:
            parsed = parse_search_query(q)
            if parsed['semester']:
                books = books.filter(semester__icontains=parsed['semester'])
            if parsed['university']:
                books = books.filter(university__icontains=parsed['university'])
            if parsed['board']:
                books = books.filter(board__icontains=parsed['board'])
            if parsed['branch']:
                books = books.filter(branch__icontains=parsed['branch'])
            search_term = parsed['keywords'] or q
            books = books.filter(
                Q(title__icontains=search_term) | Q(subject__icontains=search_term) |
                Q(author__icontains=search_term) | Q(isbn__icontains=q)
            )
        else:
            books = books.filter(
                Q(title__icontains=q) | Q(author__icontains=q) | Q(isbn__icontains=q) |
                Q(subject__icontains=q) | Q(university__icontains=q) | Q(board__icontains=q) |
                Q(branch__icontains=q) | Q(semester__icontains=q)
            )

    if category_id:
        try:
            books = books.filter(category_id=int(category_id))
        except ValueError:
            pass  # ignore a malformed value rather than 500ing the whole page
    if board:
        books = books.filter(board__icontains=board)
    if university:
        books = books.filter(university__icontains=university)
    if semester:
        books = books.filter(semester__icontains=semester)
    if condition:
        books = books.filter(condition=condition)
    if city:
        books = books.filter(pickup_city__icontains=city)
    if min_price:
        try:
            books = books.filter(price__gte=Decimal(min_price))
        except InvalidOperation:
            pass  # ignore a malformed value rather than 500ing the whole page
    if max_price:
        try:
            books = books.filter(price__lte=Decimal(max_price))
        except InvalidOperation:
            pass
    if listing_type:
        books = books.filter(listing_type=listing_type)
    if digital_type:
        books = books.filter(digital_type=digital_type)

    paginator = Paginator(books.distinct(), 12)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj': page_obj,
        'categories': Category.objects.all(),
        'query': q,
        'parsed': parsed,
        'ai_mode': ai_mode,
        'digital_type_choices': Book.DIGITAL_TYPE_CHOICES,
    }
    return render(request, 'books/book_list.html', context)


def book_detail(request, pk):
    book = get_object_or_404(Book, pk=pk)
    book.views += 1
    book.save(update_fields=['views'])

    is_wishlisted = False
    user_offer = None
    if request.user.is_authenticated:
        is_wishlisted = Wishlist.objects.filter(user=request.user, book=book).exists()
        RecentlyViewed.objects.update_or_create(user=request.user, book=book)
        user_offer = Offer.objects.filter(book=book, buyer=request.user).order_by('-created_at').first()

    similar = Book.objects.filter(
        Q(category=book.category) | Q(subject=book.subject), status='available'
    ).exclude(pk=book.pk)

    # Amazon-style "Students who bought this also bought" - real
    # collaborative filtering from past orders (see recommendations.py).
    # New/low-history listings fall back to the same subject/category pool
    # above so the section isn't empty on day one.
    also_bought_books = also_bought(book, limit=6)
    if len(also_bought_books) < 4:
        exclude_ids = {book.pk} | {b.pk for b in also_bought_books}
        fallback_needed = 6 - len(also_bought_books)
        fallback = similar.exclude(pk__in=exclude_ids)[:fallback_needed]
        also_bought_books = also_bought_books + list(fallback)

    all_offers = book.offers.select_related('buyer').order_by('-created_at') if request.user == book.seller else []

    context = {
        'book': book,
        'is_wishlisted': is_wishlisted,
        'similar': similar[:6],
        'also_bought_books': also_bought_books,
        'user_offer': user_offer,
        'all_offers': all_offers,
        'suggested_price': book.suggested_price,
    }
    return render(request, 'books/book_detail.html', context)


@login_required
@require_POST
@rate_limit('scan-cover', 20, 3600, json=True)
def scan_cover(request):
    """
    AI Book Recognition endpoint: seller uploads a cover photo, we run OCR
    and return best-guess structured fields as JSON. The frontend uses this
    to pre-fill (not auto-submit) the listing form - see add_book.html.

    OCR.space, Gemini, Google
    Vision (if set) and offline Tesseract read the cover text; an LLM (Gemini/Groq) then sorts it into fields - see
    ocr_utils.scan_cover_image().
    """
    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': 'No image uploaded.'}, status=400)
    if image.size > 8 * 1024 * 1024:
        return JsonResponse({'error': 'Image too large (max 8MB).'}, status=400)

    try:
        info = scan_cover_image(image)
    except Exception:
        return JsonResponse({
            'error': 'Could not process this image. Please try a clearer photo or fill in the form manually.'
        }, status=422)

    return JsonResponse(info)


@login_required
@require_POST
@rate_limit('scan-isbn', 30, 3600, json=True)
def scan_isbn(request):
    """
    Barcode scanner endpoint: the frontend decodes the ISBN barcode itself
    (via the device camera, client-side) or the seller types it in, then
    posts the ISBN here. We look it up against public book databases and
    return structured fields to pre-fill the listing form - faster and more
    reliable than OCR, since a barcode is an exact number, not text to guess.
    """
    isbn = request.POST.get('isbn', '')
    if not isbn.strip():
        return JsonResponse({'error': 'No ISBN provided.'}, status=400)
    result = lookup_isbn(isbn)
    return JsonResponse(result)


@login_required
@require_POST
@rate_limit('price-rec', 30, 3600, json=True)
def price_recommendation(request):
    """
    AI Price Recommendation endpoint for the sell/edit listing form: seller
    fills in title/condition/edition/etc as they go, the frontend posts the
    in-progress field values here (see add_book.html), and we return a
    Market Price + Recommended Price range with a factor breakdown. See
    price_recommendation.recommend_price() for the actual logic.
    """
    def _decimal_or_none(raw):
        try:
            return Decimal(raw) if raw not in (None, '') else None
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _int_or_none(raw):
        try:
            return int(raw) if raw not in (None, '') else None
        except (TypeError, ValueError):
            return None

    title = request.POST.get('title', '').strip()
    if not title:
        return JsonResponse({'error': 'Enter a title first so we can look up comparable books.'}, status=400)

    result = recommend_price(
        title=title,
        author=request.POST.get('author', ''),
        isbn=request.POST.get('isbn', ''),
        edition=request.POST.get('edition', ''),
        condition=request.POST.get('condition', 'good'),
        publication_year=_int_or_none(request.POST.get('publication_year')),
        original_price=_decimal_or_none(request.POST.get('original_price')),
        category_id=_int_or_none(request.POST.get('category')),
        subject=request.POST.get('subject', ''),
        exclude_pk=_int_or_none(request.POST.get('book_id')),
    )
    return JsonResponse({
        'market_price': str(result['market_price']),
        'recommended_low': str(result['recommended_low']),
        'recommended_high': str(result['recommended_high']),
        'confidence': result['confidence'],
        'factors': result['factors'],
    })


@login_required
@require_POST
@rate_limit('check-dup', 60, 3600, json=True)
def check_duplicate(request):
    """
    Live duplicate-listing check for the sell/edit form (AJAX) - see
    add_book.html. Fires as the seller types the title/ISBN so they find out
    *before* submitting that they already have this book listed, instead of
    only being told after clicking Publish.
    """
    def _int_or_none(raw):
        try:
            return int(raw) if raw not in (None, '') else None
        except (TypeError, ValueError):
            return None

    dupes = find_duplicate_listings(
        request.user,
        title=request.POST.get('title', ''),
        isbn=request.POST.get('isbn', ''),
        author=request.POST.get('author', ''),
        exclude_pk=_int_or_none(request.POST.get('book_id')),
    )
    return JsonResponse({
        'duplicate': dupes.exists(),
        'listings': [
            {'id': b.pk, 'title': b.title, 'url': b.get_absolute_url(), 'status': b.get_status_display()}
            for b in dupes[:5]
        ],
    })


@login_required
def add_book(request):
    if not request.user.is_seller_verified():
        messages.warning(
            request,
            _('Verify your email before listing a book for sale.'),
        )
        return redirect('accounts:verify_seller')

    duplicates = []
    if request.method == 'POST':
        form = BookForm(request.POST, request.FILES)
        formset = BookImageFormSet(request.POST, request.FILES, instance=Book())
        if form.is_valid():
            # Server-side backstop for the live AJAX check: skipped only if
            # the seller already saw the warning below and clicked
            # "Publish Anyway" (confirm_duplicate=1), so this can't be
            # bypassed by a client with JS disabled.
            if request.POST.get('confirm_duplicate') != '1':
                duplicates = list(find_duplicate_listings(
                    request.user,
                    title=form.cleaned_data.get('title', ''),
                    isbn=form.cleaned_data.get('isbn', ''),
                    author=form.cleaned_data.get('author', ''),
                ))
            if duplicates:
                messages.warning(
                    request,
                    _('Already listed - you already have an active listing that looks like this book. '
                    'Review it below, or publish anyway if this is a different copy.'),
                )
            else:
                book = form.save(commit=False)
                book.seller = request.user
                book.save()
                formset = BookImageFormSet(request.POST, request.FILES, instance=book)
                if formset.is_valid():
                    formset.save()

                # AI Fraud Detection - runs after the images are actually
                # saved so the image-reuse check can hash the real files.
                # A high-risk listing is held as 'under_review' instead of
                # going live/matching saved searches - see fraud_detection.py.
                fraud_result = run_fraud_scan(
                    seller=request.user,
                    title=book.title,
                    description=book.description,
                    price=book.price,
                    isbn=book.isbn,
                    category_id=book.category_id,
                    subject=book.subject,
                    images=[bi.image for bi in book.images.all()],
                )
                book.set_fraud_result(fraud_result)
                if fraud_result['hold_for_review']:
                    book.status = 'under_review'
                book.save(update_fields=['fraud_score', 'fraud_flags', 'fraud_checked_at', 'status'])

                if fraud_result['hold_for_review']:
                    messages.warning(
                        request,
                        _("Your listing was saved but held for manual review by our Fraud Detection system "
                        "before it goes live to buyers - we'll notify you once it's approved."),
                    )
                else:
                    # Push notifications for anyone waiting on a book like
                    # this one - fired here, at the moment the listing goes
                    # live, same pattern as the rest of bookmarket/notify.py's
                    # calls. Skipped for held listings since they aren't
                    # actually visible to buyers yet.
                    for saved_search in find_matching_saved_searches(book):
                        notify_saved_search_match(saved_search, book)
                    for book_request in find_matching_book_requests(book):
                        notify_request_fulfilled(book_request, book)
                    messages.success(request, _('Your book has been listed for sale!'))
                return redirect('books:detail', pk=book.pk)
    else:
        form = BookForm()
        formset = BookImageFormSet(instance=Book())
    return render(request, 'books/add_book.html', {'form': form, 'formset': formset, 'duplicates': duplicates})


@login_required
def edit_book(request, pk):
    book = get_object_or_404(Book, pk=pk, seller=request.user)
    old_price = book.price
    duplicates = []
    if request.method == 'POST':
        form = BookForm(request.POST, request.FILES, instance=book)
        formset = BookImageFormSet(request.POST, request.FILES, instance=book)
        if form.is_valid() and formset.is_valid():
            if request.POST.get('confirm_duplicate') != '1':
                duplicates = list(find_duplicate_listings(
                    request.user,
                    title=form.cleaned_data.get('title', ''),
                    isbn=form.cleaned_data.get('isbn', ''),
                    author=form.cleaned_data.get('author', ''),
                    exclude_pk=book.pk,
                ))
            if duplicates:
                messages.warning(
                    request,
                    _('Already listed - you have another active listing that looks like this book. '
                    'Review it below, or save anyway if this is a different copy.'),
                )
            else:
                form.save()
                formset.save()

                fraud_result = run_fraud_scan(
                    seller=request.user,
                    title=book.title,
                    description=book.description,
                    price=book.price,
                    isbn=book.isbn,
                    category_id=book.category_id,
                    subject=book.subject,
                    images=[bi.image for bi in book.images.all()],
                    exclude_pk=book.pk,
                )
                book.set_fraud_result(fraud_result)
                # An edit that now trips the fraud checks pulls a
                # previously-live listing back for review too; an edit
                # that clears the checks on a held listing doesn't
                # auto-republish it - an admin still signs off, same as a
                # first-time high-risk listing.
                if fraud_result['hold_for_review']:
                    book.status = 'under_review'
                book.save(update_fields=['fraud_score', 'fraud_flags', 'fraud_checked_at', 'status'])

                if fraud_result['hold_for_review']:
                    messages.warning(
                        request,
                        _("Your changes were saved but this listing is now held for manual review by our "
                        "Fraud Detection system before it's visible to buyers again."),
                    )
                else:
                    messages.success(request, _('Listing updated.'))
                    if book.price < old_price:
                        notify_price_drop(book, old_price)
                return redirect('books:detail', pk=book.pk)
    else:
        form = BookForm(instance=book)
        formset = BookImageFormSet(instance=book)
    return render(request, 'books/add_book.html', {
        'form': form, 'formset': formset, 'editing': True, 'book': book, 'duplicates': duplicates,
    })


@login_required
def delete_book(request, pk):
    book = get_object_or_404(Book, pk=pk, seller=request.user)
    if request.method == 'POST':
        book.status = 'removed'
        book.save(update_fields=['status'])
        messages.info(request, _('Listing removed.'))
        return redirect('accounts:dashboard')
    return render(request, 'books/confirm_delete.html', {'book': book})


@login_required
def report_book(request, pk):
    """Buyer-facing counterpart to the automated Fraud Detection scan -
    lets anyone flag a listing they think is fake/spam/wrong for a human
    to look at, without being able to take it down themselves. See
    books.models.ListingReport and adminpanel/views.py for how staff
    act on these."""
    book = get_object_or_404(Book, pk=pk)
    if book.seller_id == request.user.pk:
        messages.error(request, _("You can't report your own listing."))
        return redirect('books:detail', pk=book.pk)

    if ListingReport.objects.filter(book=book, reporter=request.user).exists():
        messages.info(request, _("You've already reported this listing - our team is on it."))
        return redirect('books:detail', pk=book.pk)

    if request.method == 'POST':
        form = ListingReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.book = book
            report.reporter = request.user
            report.save()
            messages.success(request, _('Thanks - our team will take a look.'))
            return redirect('books:detail', pk=book.pk)
    else:
        form = ListingReportForm()
    return render(request, 'books/report_listing.html', {'book': book, 'form': form})


# (days, price) - kept as a simple ordered tuple (like wallet.services'
# reward constants) rather than a DB table, since these are business
# choices staff would tweak in code, not something sellers configure.
FEATURED_BUMP_OPTIONS = [
    (3, Decimal('29')),
    (7, Decimal('59')),
    (14, Decimal('99')),
]


@login_required
def bump_listing(request, pk):
    """Lets a seller pay (from their BookBridge Wallet) to feature their
    own listing for a few days - the seller-facing purchase flow for the
    is_featured/featured_until hooks that previously only Django admin
    could set. See Book.is_currently_featured() and
    FEATURED_BUMP_OPTIONS above for the options/pricing.

    There's no wallet top-up flow yet (wallet balance only comes from
    cashback/referrals/refunds - see wallet/services.py), so a seller
    with too little balance is pointed at their wallet page rather than
    a payment gateway here.
    """
    book = get_object_or_404(Book, pk=pk, seller=request.user)
    wallet = get_wallet(request.user)

    if request.method == 'POST':
        try:
            days = int(request.POST.get('days', ''))
        except ValueError:
            days = None
        price = dict(FEATURED_BUMP_OPTIONS).get(days)
        if price is None:
            messages.error(request, _('Choose one of the listed bump options.'))
            return redirect('books:bump', pk=book.pk)
        if book.status != 'available':
            messages.error(request, _('Only a live listing can be featured.'))
            return redirect('books:detail', pk=book.pk)

        try:
            wallet_debit(
                request.user, price, 'featured_listing',
                description=f'{days}-day featured bump for "{book.title}"',
            )
        except InsufficientBalance:
            messages.error(
                request,
                _("Your wallet balance (₹%(balance)s) isn't enough to cover this ₹%(price)s bump. Wallet balance comes from cashback, referrals, and refunds.") % {'balance': wallet.balance, 'price': price},
            )
            return redirect('wallet:detail')

        # Stacks onto any bump time already remaining, rather than
        # resetting the clock, so buying another bump before the first
        # one expires doesn't waste the days already paid for.
        base = book.featured_until if book.featured_until and book.featured_until > timezone.now() else timezone.now()
        book.featured_until = base + timezone.timedelta(days=days)
        book.save(update_fields=['featured_until'])
        messages.success(request, _('"%(title)s" will be featured until %(featured_until)s.') % {'title': book.title, 'featured_until': date_format(book.featured_until, 'd M Y')})
        return redirect('books:detail', pk=book.pk)

    return render(request, 'books/bump_listing.html', {
        'book': book, 'wallet': wallet, 'options': FEATURED_BUMP_OPTIONS,
    })


@login_required
def toggle_wishlist(request, pk):
    book = get_object_or_404(Book, pk=pk)
    wl, created = Wishlist.objects.get_or_create(user=request.user, book=book)
    if not created:
        wl.delete()
        messages.info(request, _('Removed from wishlist.'))
    else:
        messages.success(request, _('Added to wishlist.'))
    return redirect('books:detail', pk=pk)


@login_required
def wishlist_view(request):
    items = Wishlist.objects.filter(user=request.user).select_related('book')
    return render(request, 'books/wishlist.html', {'items': items})


@login_required
@require_POST
def save_search(request):
    """
    Saves the current book_list filters (posted as hidden fields mirroring
    the GET params on that page) as a SavedSearch, so the buyer gets
    notified next time a matching book is listed instead of re-checking.
    """
    def _decimal_or_none(raw):
        raw = (raw or '').strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation:
            return None

    category_id = request.POST.get('category') or None
    SavedSearch.objects.create(
        user=request.user,
        query=request.POST.get('q', '').strip(),
        category_id=category_id,
        board=request.POST.get('board', '').strip(),
        university=request.POST.get('university', '').strip(),
        semester=request.POST.get('semester', '').strip(),
        condition=request.POST.get('condition', '').strip(),
        city=request.POST.get('city', '').strip(),
        min_price=_decimal_or_none(request.POST.get('min_price')),
        max_price=_decimal_or_none(request.POST.get('max_price')),
        listing_type=request.POST.get('listing_type', '').strip(),
        digital_type=request.POST.get('digital_type', '').strip(),
    )
    messages.success(request, _("Search saved - we'll email you when a matching book is listed."))
    return redirect(safe_next_url(request, 'books:list'))


@login_required
def saved_search_list(request):
    searches = SavedSearch.objects.filter(user=request.user)
    return render(request, 'books/saved_search_list.html', {'searches': searches})


@login_required
@require_POST
def delete_saved_search(request, pk):
    search = get_object_or_404(SavedSearch, pk=pk, user=request.user)
    search.delete()
    messages.info(request, _('Saved search removed.'))
    return redirect('books:saved_search_list')


def request_list(request):
    requests_qs = BookRequest.objects.filter(status='open')
    return render(request, 'books/request_list.html', {'requests': requests_qs})


@login_required
def add_request(request):
    if request.method == 'POST':
        form = BookRequestForm(request.POST)
        if form.is_valid():
            req = form.save(commit=False)
            req.buyer = request.user
            req.save()
            messages.success(
                request,
                _("Your request has been posted! We'll email you the moment a seller lists a matching book.")
            )
            return redirect('books:request_list')
    else:
        form = BookRequestForm()
    return render(request, 'books/add_request.html', {'form': form})


@login_required
def matching_requests_for_seller(request):
    """Simple matching: shows open requests whose title/isbn overlaps this seller's stock or general interest."""
    my_books_titles = list(Book.objects.filter(seller=request.user).values_list('title', flat=True))
    requests_qs = BookRequest.objects.filter(status='open')
    matched = []
    other = []
    for r in requests_qs:
        hit = any(title_overlap(r.title, t) for t in my_books_titles)
        (matched if hit else other).append(r)
    return render(request, 'books/matching_requests.html', {'matched': matched, 'other': other})


@login_required
def bulk_upload_books(request):
    """Lets a verified seller list many physical books at once from a CSV
    or Excel file instead of one-by-one via `add_book` - see
    books/bulk_import.py for the parsing/validation/creation logic."""
    if not request.user.is_seller_verified():
        messages.warning(
            request,
            _('Verify your email before listing books for sale.'),
        )
        return redirect('accounts:verify_seller')

    result = None
    if request.method == 'POST':
        form = BulkUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                result = process_bulk_upload(request.FILES['file'], request.user)
            except BulkUploadFileError as exc:
                form.add_error('file', str(exc))
            else:
                if result['created']:
                    held_count = sum(1 for b in result['created'] if b.status == 'under_review')
                    if held_count:
                        messages.success(
                            request,
                            _('%(count)s book(s) imported - %(held_count)s were held for manual review by our Fraud Detection system before going live.') % {'count': len(result['created']), 'held_count': held_count},
                        )
                    else:
                        messages.success(request, _('%(count)s book(s) listed for sale!') % {'count': len(result['created'])})
                if result['errors']:
                    messages.warning(
                        request,
                        _("%(count)s row(s) couldn't be imported - see details below.") % {'count': len(result['errors'])},
                    )
                if not result['created'] and not result['errors']:
                    messages.info(request, _('No rows found in that file.'))
    else:
        form = BulkUploadForm()
    return render(request, 'books/bulk_upload.html', {
        'form': form, 'result': result, 'template_headers': TEMPLATE_HEADERS,
    })


@login_required
def bulk_upload_template(request):
    """Downloadable .xlsx template matching bulk_upload_books' expected columns."""
    workbook = build_template_workbook()
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="bookbridge_bulk_upload_template.xlsx"'
    workbook.save(response)
    return response
