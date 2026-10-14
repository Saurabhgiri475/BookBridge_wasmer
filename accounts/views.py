import logging
import json

from django.conf import settings

from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib.auth.views import LoginView, LogoutView, PasswordResetConfirmView, PasswordResetView
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.db import transaction
from django.db.models import Count, Q, Sum, Avg
from django.urls import reverse, reverse_lazy
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django.utils.translation import gettext as _, ngettext

from partners.forms import PartnerApplicationForm
from .validators import PROOF_HELP_TEXT
from partners.models import ShopkeeperPartner
from .forms import (
    BookBridgeAuthenticationForm, BookBridgeSetPasswordForm, OTPForm, ProfileForm, SignUpForm, mark_invalid_fields,
)
from .models import MAX_OTP_ATTEMPTS, OTP_VALIDITY_MINUTES, User
from .fraud_detection import find_linked_accounts
from . import throttle
from bookmarket.security import rate_limit
from books.models import Book
from orders.models import Order, Review
from orders.utils import generate_otp
from bookmarket.notify import notify_signup_welcome, notify_email_otp, notify_password_changed
from bookmarket.sustainability import compute_impact, badges_earned, next_badge
from donations.services import books_donated_by
from wallet.services import credit as wallet_credit, signup_bonus_allowed, REFERRAL_SIGNUP_BONUS


PENDING_SIGNUP_SESSION_KEY = 'pending_signup_user_id'


class BookBridgeLoginView(LoginView):
    """Normal login, except approved Shopkeeper Partners land on their Partner
    Dashboard (unless they were sent here from a specific page via ?next=)."""
    template_name = 'accounts/login.html'
    authentication_form = BookBridgeAuthenticationForm

    def _login_throttle_keys(self):
        username = (self.request.POST.get('username') or '').strip().lower()
        ip = throttle.get_client_ip(self.request)
        return [
            ('login-fail-user', username, settings.LOGIN_MAX_FAILURES_PER_USERNAME),
            ('login-fail-ip', ip, settings.LOGIN_MAX_FAILURES_PER_IP),
        ]

    def post(self, request, *args, **kwargs):
        # Brute-force guard: once an account name (or an IP) has used up its
        # failed-attempt budget, stop checking passwords at all for a while.
        if settings.RATE_LIMITING_ENABLED:
            window = settings.LOGIN_FAILURE_WINDOW_SECONDS
            for scope, ident, limit in self._login_throttle_keys():
                if ident and throttle.peek(scope, ident, limit, window):
                    form = self.get_form()
                    form.add_error(None, 'Too many failed sign-in attempts. Please wait about '
                                         '15 minutes and try again, or use "Forgot your password?" below.')
                    return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if settings.RATE_LIMITING_ENABLED:
            username = (self.request.POST.get('username') or '').strip().lower()
            throttle.reset('login-fail-user', username, settings.LOGIN_FAILURE_WINDOW_SECONDS)
        return super().form_valid(form)

    def form_invalid(self, form):
        # Right password, but the signup email code was never entered.
        if form.unverified_user:
            self.request.session[PENDING_SIGNUP_SESSION_KEY] = form.unverified_user.pk
            messages.info(self.request, _("Your email isn't verified yet - enter the code we emailed you to activate your account."))
            return redirect('accounts:verify_signup')
        if settings.RATE_LIMITING_ENABLED:
            for scope, ident, limit in self._login_throttle_keys():
                if ident:
                    throttle.hit(scope, ident, limit, settings.LOGIN_FAILURE_WINDOW_SECONDS)
        return super().form_invalid(form)

    def get_success_url(self):
        if self.get_redirect_url():
            return super().get_success_url()
        partner = ShopkeeperPartner.objects.filter(user=self.request.user).first()
        if partner and partner.is_approved:
            return reverse('partners:dashboard')
        return super().get_success_url()


class BookBridgePasswordResetView(PasswordResetView):
    """Step 1: the user enters their email and we send a one-hour link.

    The answer never depends on whether the address belongs to an account (so the
    form can't be used to discover who is registered), and the number of emails is
    capped per address and per network. Unverified signups (is_active=False) and
    accounts without a usable password get no email - Django's form skips them.
    """
    template_name = 'accounts/password_reset_form.html'
    email_template_name = 'accounts/emails/password_reset_email.txt'
    subject_template_name = 'accounts/emails/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password_reset_done')

    def post(self, request, *args, **kwargs):
        if settings.RATE_LIMITING_ENABLED:
            allowed, wait = throttle.hit(
                'pw-reset-ip', throttle.get_client_ip(request), settings.PASSWORD_RESET_MAX_PER_IP_PER_HOUR, 3600,
            )
            if not allowed:
                form = self.get_form()
                form.add_error(None, f'Too many reset requests from your network. Please try again in {throttle.human_wait(wait)}.')
                return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if settings.RATE_LIMITING_ENABLED:
            email = form.cleaned_data['email'].strip().lower()
            allowed, _wait = throttle.hit('pw-reset-email', email, settings.PASSWORD_RESET_MAX_PER_EMAIL_PER_HOUR, 3600)
            if not allowed:
                # Same page as success: don't reveal that this address was throttled (or exists).
                return redirect(self.get_success_url())
        # The email is rendered without a request, so context processors don't run -
        # pass the support contact in explicitly.
        from bookmarket.context_processors import support_contact
        self.extra_email_context = support_contact(self.request)
        return super().form_valid(form)


class BookBridgePasswordResetConfirmView(PasswordResetConfirmView):
    """Step 2: the emailed link. Sets the new password, lifts any login lockout and
    tells the owner by email that the password changed."""
    template_name = 'accounts/password_reset_confirm.html'
    form_class = BookBridgeSetPasswordForm
    success_url = reverse_lazy('accounts:password_reset_complete')

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        # Changing the password also signs out every other session (the session
        # hash is derived from the password), so a stolen session stops working.
        if settings.RATE_LIMITING_ENABLED:
            throttle.reset('login-fail-user', user.get_username().lower(), settings.LOGIN_FAILURE_WINDOW_SECONDS)
        notify_password_changed(user)
        return response


def _signup_blocked_message(request):
    """Rate-limit gate for the signup POST. Returns a message when blocked, else None."""
    ip = throttle.get_client_ip(request)
    allowed, wait = throttle.hit('signup-attempt', ip, settings.SIGNUP_MAX_ATTEMPTS_PER_IP_PER_HOUR, 3600)
    if not allowed:
        return _('Too many signup attempts from your network. Please try again in %(wait)s.') % {'wait': throttle.human_wait(wait)}
    if throttle.peek('signup-created', ip, settings.SIGNUP_MAX_ACCOUNTS_PER_IP_PER_HOUR, 3600):
        return _('Several accounts were just created from your network. Please try again in a while, or log in if you already signed up.')
    return None


def signup(request):
    wants_partner = request.POST.get('apply_as_partner') == 'on' if request.method == 'POST' else bool(request.GET.get('partner'))
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        # The shop details are only required (and only validated) when the
        # "become a Partner" box is ticked.
        partner_form = PartnerApplicationForm(request.POST, request.FILES) if wants_partner else PartnerApplicationForm()

        blocked = _signup_blocked_message(request)
        if blocked:
            messages.error(request, blocked)
            status = 429
        elif throttle.looks_like_bot(request.POST):
            # Don't tell a bot what tripped. A real person who was just very quick gets a retry.
            messages.error(request, _('Something went wrong submitting the form - please wait a moment and try again.'))
            status = 400
        elif not throttle.verify_captcha(request):
            messages.error(request, _('Please complete the human-check below and try again.'))
            status = 400
        else:
            status = 200

        if status == 200 and form.is_valid() and (not wants_partner or partner_form.is_valid()):
            with transaction.atomic():
                user = form.save(commit=False)
                referral_code = form.cleaned_data.get('referral_code')
                referrer = User.objects.filter(referral_code=referral_code).first() if referral_code else None
                if referrer:
                    user.referred_by = referrer
                # The account only comes alive once the emailed code is entered
                # (verify_signup) - so a made-up address can never log in, and
                # the referral bonus / welcome email wait until then too.
                user.is_active = False
                user.signup_pending = True
                user.save()
                signup_otp = generate_otp()
                user.set_otp('email_otp', signup_otp)
                user.email_otp_sent_at = timezone.now()
                user.email_otp_attempts = 0
                user.save(update_fields=['email_otp', 'email_otp_sent_at', 'email_otp_attempts'])

                if wants_partner:
                    partner = partner_form.save(commit=False)
                    partner.user = user
                    partner.save()

            # Counted only now, so typos and validation errors don't use up the account budget.
            throttle.hit('signup-created', throttle.get_client_ip(request), settings.SIGNUP_MAX_ACCOUNTS_PER_IP_PER_HOUR, 3600)
            sent = False
            email_ok, _wait = throttle.hit('signup-email', user.email.lower(), settings.SIGNUP_MAX_PER_EMAIL_PER_DAY, 86400)
            if email_ok:
                sent = notify_email_otp(user, signup_otp)
            request.session[PENDING_SIGNUP_SESSION_KEY] = user.pk
            if wants_partner:
                messages.success(request, _('Your Partner application form has been submitted!'))
            if sent:
                messages.info(request, _('We emailed a 6-digit code to %(email)s. Enter it below to activate your account.') % {'email': _mask_email(user.email)})
            else:
                messages.warning(request, _("We couldn't send the code just now - use \"Resend code\" below."))
            return redirect('accounts:verify_signup')
        response_status = status
    else:
        initial = {}
        ref = request.GET.get('ref', '').strip().upper()
        if ref and User.objects.filter(referral_code=ref).exists():
            initial['referral_code'] = ref
        if wants_partner:
            initial['apply_as_partner'] = True
        form = SignUpForm(initial=initial)
        partner_form = PartnerApplicationForm()
        response_status = 200
    mark_invalid_fields(form, partner_form)
    return render(request, 'accounts/signup.html', {
        'form': form, 'partner_form': partner_form, 'open_partner': wants_partner,
        'form_token': throttle.make_form_token(), 'honeypot_field': throttle.HONEYPOT_FIELD,
        'form_token_field': throttle.FORM_TOKEN_FIELD,
        'turnstile_site_key': settings.TURNSTILE_SITE_KEY if throttle.captcha_enabled() else '',
        'proof_help': PROOF_HELP_TEXT,
    }, status=response_status)


@require_GET
def check_availability(request):
    """Live username/email availability for the signup form (static/js/auth-ui.js).

    Always answers 200 with {available, message} - the page treats this as a hint
    only; the real check still happens on submit (SignUpForm). Throttled per IP so
    it can't be used to harvest which emails are registered. The email answer matches
    SignUpForm.clean_email: accounts still waiting for their code don't count."""
    ip = throttle.get_client_ip(request)
    allowed, wait = throttle.hit('availability', ip, settings.AVAILABILITY_CHECKS_PER_IP_PER_MINUTE, 60)
    if not allowed:
        resp = JsonResponse({'available': None, 'message': 'Checking too fast - please wait a moment.'}, status=429)
        resp['Retry-After'] = str(wait)
        return resp

    field = request.GET.get('field', '')
    value = (request.GET.get('value') or '').strip()
    if field == 'username':
        if not value:
            return JsonResponse({'available': None, 'message': ''})
        try:
            User._meta.get_field('username').run_validators(value)
        except ValidationError as exc:
            return JsonResponse({'available': False, 'message': exc.messages[0]})
        if len(value) > 150:
            return JsonResponse({'available': False, 'message': 'Usernames can be at most 150 characters.'})
        taken = User.objects.filter(username__iexact=value).exists()
        return JsonResponse({'available': not taken, 'message': 'That username is taken - try another.' if taken else 'Username is available.'})
    if field == 'email':
        if not value:
            return JsonResponse({'available': None, 'message': ''})
        try:
            validate_email(value)
        except ValidationError:
            return JsonResponse({'available': False, 'message': 'Enter a valid email address.'})
        taken = User.objects.filter(email__iexact=value, signup_pending=False).exists()
        return JsonResponse({
            'available': not taken,
            'message': 'An account with this email already exists - try logging in.' if taken else 'Email looks good.',
        })
    return JsonResponse({'available': None, 'message': ''}, status=400)


def _mask_email(email):
    local, _, domain = email.partition('@')
    return f"{local[:1]}{'*' * max(len(local) - 1, 2)}@{domain}"


def _pending_signup_user(request):
    """The not-yet-verified account this browser session just signed up (or tried
    to log in) as. Only accounts still flagged signup_pending qualify - never an
    active user, and never one an admin deactivated."""
    uid = request.session.get(PENDING_SIGNUP_SESSION_KEY)
    if not uid:
        return None
    return User.objects.filter(pk=uid, signup_pending=True, is_active=False).first()


def _complete_signup(request, user):
    """Everything that used to happen at the moment of signup, now run once the
    email is confirmed: referral welcome bonus, login, welcome email, messages."""
    referrer = user.referred_by
    bonus_given = False
    if referrer and signup_bonus_allowed(user, referrer):
        bonus_given = True
        wallet_credit(
            user, REFERRAL_SIGNUP_BONUS, 'referral',
            description=f"Welcome bonus - signed up via {referrer.username}'s referral link",
            idempotency_key=f'signup-bonus:{user.pk}',
        )
    login(request, user)
    request.session.pop(PENDING_SIGNUP_SESSION_KEY, None)
    notify_signup_welcome(user)

    has_partner_application = ShopkeeperPartner.objects.filter(user=user).exists()
    if bonus_given:
        messages.success(
            request,
            _('Welcome to BookBridge! \u20b9%(amount)s referral bonus has been added to your wallet.') % {'amount': REFERRAL_SIGNUP_BONUS},
        )
    if has_partner_application:
        messages.success(
            request,
            _('Email verified - your account is ready and your Partner application form has been submitted! '
              'Our team will review your documents - until then you can use BookBridge like any other member.'),
        )
        return redirect('partners:apply')
    if not referrer:
        messages.success(request, _('Email verified - welcome to BookBridge! Your account is ready.'))
    return redirect('books:home')


def verify_signup(request):
    """Step 2 of signup: enter the 6-digit code emailed to the new address."""
    if request.user.is_authenticated:
        return redirect('books:home')
    user = _pending_signup_user(request)
    if not user:
        request.session.pop(PENDING_SIGNUP_SESSION_KEY, None)
        messages.info(request, _('Log in, or sign up to get a new verification code.'))
        return redirect('accounts:login')

    if request.method == 'POST':
        action = request.POST.get('action', 'verify')

        if action == 'cancel':
            # Typo in the email? Drop the unverified account (and any partner
            # application that came with it) so they can start over.
            user.delete()
            request.session.pop(PENDING_SIGNUP_SESSION_KEY, None)
            messages.info(request, _('Your unverified signup was discarded - you can sign up again.'))
            return redirect('accounts:signup')

        if action == 'resend':
            if not User.can_resend_otp(user.email_otp_sent_at):
                messages.error(request, _('Please wait a minute before requesting another code.'))
            else:
                otp = generate_otp()
                user.set_otp('email_otp', otp)
                user.email_otp_sent_at = timezone.now()
                user.email_otp_attempts = 0
                user.save(update_fields=['email_otp', 'email_otp_sent_at', 'email_otp_attempts'])
                if notify_email_otp(user, otp):
                    messages.success(request, _('A new code was sent to %(email)s.') % {'email': _mask_email(user.email)})
                else:
                    messages.warning(request, _("We couldn't send the code just now - please try again shortly."))
            return redirect('accounts:verify_signup')

        # action == 'verify'
        form = OTPForm(request.POST)
        if not form.is_valid():
            messages.error(request, _('Enter the 6-digit code from your email.'))
            return redirect('accounts:verify_signup')
        if user.check_email_otp(form.cleaned_data['otp']):
            if User.objects.filter(email__iexact=user.email, signup_pending=False).exclude(pk=user.pk).exists():
                messages.error(request, _('Another account already uses this email. Start over with a different one.'))
                return redirect('accounts:verify_signup')
            user.is_active = True
            user.signup_pending = False
            user.email_verified = True
            user.email_otp = ''
            user.save(update_fields=['is_active', 'signup_pending', 'email_verified', 'email_otp'])
            return _complete_signup(request, user)

        user.refresh_from_db(fields=['email_otp_attempts'])
        if user.email_otp_attempts >= MAX_OTP_ATTEMPTS:
            messages.error(request, _('Too many wrong attempts. Request a new code below.'))
        else:
            left = MAX_OTP_ATTEMPTS - user.email_otp_attempts
            messages.error(request, ngettext(
                'That code is incorrect or has expired. %(left)d attempt left.',
                'That code is incorrect or has expired. %(left)d attempts left.', left) % {'left': left})
        return redirect('accounts:verify_signup')

    return render(request, 'accounts/verify_signup.html', {
        'masked_email': _mask_email(user.email), 'validity_minutes': OTP_VALIDITY_MINUTES,
        'can_resend': User.can_resend_otp(user.email_otp_sent_at),
    })


@login_required
def profile(request, username=None):
    user = get_object_or_404(User, username=username) if username else request.user
    is_owner = user == request.user
    books = Book.objects.filter(seller=user, status='available').order_by('-created_at')
    reviews = Review.objects.filter(order__book__seller=user).order_by('-created_at')[:10]

    # Personal sustainability impact: every delivered order where this user
    # was either the buyer or the seller represents one book that got a
    # second life instead of a new one being printed. Same coefficients as
    # the community dashboard (bookmarket/sustainability.py), so the numbers
    # always add up consistently across the site.
    books_bought = Order.objects.filter(buyer=user, status='delivered').count()
    books_sold = Order.objects.filter(book__seller=user, status='delivered').count()
    books_donated = books_donated_by(user)  # verified-received donations also give books a second life
    books_reused = books_bought + books_sold + books_donated

    context = {
        'profile_user': user,
        'is_owner': is_owner,
        'books': books,
        'reviews': reviews,
        'seller_rating': user.seller_rating(),
        'buyer_rating': user.buyer_rating(),
        'books_bought': books_bought,
        'books_sold': books_sold,
        'books_donated': books_donated,
        'impact': compute_impact(books_reused),
        'earned_badges': badges_earned(books_reused),
        'next_badge': next_badge(books_reused),
    }
    return render(request, 'accounts/profile.html', context)


@login_required
def profile_by_id(request, pk):
    """Profile page addressed by user id, so public links never put a username (which, on older
    accounts, may be an email address) in the URL."""
    user = get_object_or_404(User, pk=pk)
    return profile(request, username=user.username)


@login_required
def edit_profile(request):
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():  # savepoint: a failed save must not poison the request's transaction
                    form.save()
            except Exception:
                # A new photo that cannot be written (read-only / ephemeral disk, bucket not set up or unreachable)
                # used to surface as a bare 500. Keep that case a normal form error; anything else is a real bug.
                if 'avatar' not in request.FILES:
                    raise
                logging.getLogger(__name__).exception('Profile photo could not be stored')
                request.user.refresh_from_db()
                form = ProfileForm(request.POST, instance=request.user)
                form.is_valid()
                photo_error = _("We couldn't store your photo right now. Save your other details without it, or try again later.")
                form.add_error('avatar', photo_error)
                messages.error(request, photo_error)  # edit_profile.html does not print field errors
            else:
                messages.success(request, _('Profile updated.'))
                return redirect('accounts:profile')
    else:
        form = ProfileForm(instance=request.user)
    return render(request, 'accounts/edit_profile.html', {'form': form})


@login_required
def dashboard(request):
    user = request.user
    my_books = Book.objects.filter(seller=user)
    sales = Order.objects.filter(book__seller=user)
    purchases = Order.objects.filter(buyer=user)

    stats = {
        'listed': my_books.count(),
        'available': my_books.filter(status='available').count(),
        'sold': my_books.filter(status='sold').count(),
        'pending_orders': sales.filter(status__in=['placed', 'accepted', 'shipped']).count(),
        'revenue': sum(o.final_price for o in sales.filter(status='delivered')),
        'total_purchases': purchases.count(),
    }
    # Listing Performance: turns the views/offers Book already tracks into
    # something a seller can act on (see templates/accounts/dashboard.html)
    # - e.g. "lots of views, zero offers" is a nudge to lower the price,
    # rather than silent numbers nobody ever sees.
    listing_performance = my_books.filter(status='available').annotate(
        offer_count=Count('offers', distinct=True),
        wishlist_count=Count('wishlisted_by', distinct=True),
    ).order_by('-views')[:10]

    context = {
        'stats': stats,
        'recent_sales': sales.order_by('-created_at')[:8],
        'recent_purchases': purchases.order_by('-created_at')[:8],
        'my_books': my_books.order_by('-created_at')[:8],
        'listing_performance': listing_performance,
    }
    return render(request, 'accounts/dashboard.html', context)


# --- Seller analytics ------------------------------------------------------
# Charting on data the app already tracks (orders, wallet payouts, reviews) -
# nothing new is computed here, it's just grouped by month/category for the
# dashboard.html "View Analytics" link. Chart.js (loaded in the template)
# renders the three series below straight from analytics_data's JSON.

ANALYTICS_MONTHS_BACK = 12


@login_required
def seller_analytics(request):
    user = request.user
    since = timezone.now() - timezone.timedelta(days=30 * ANALYTICS_MONTHS_BACK)

    delivered_sales = Order.objects.filter(book__seller=user, status='delivered')

    sales_over_time = list(
        delivered_sales.filter(created_at__gte=since)
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(revenue=Sum('final_price'), orders=Count('id'))
        .order_by('month')
    )

    best_categories = list(
        delivered_sales.values('book__category__name')
        .annotate(revenue=Sum('final_price'), orders=Count('id'))
        .order_by('-revenue')[:8]
    )

    rating_trend = list(
        Review.objects.filter(order__book__seller=user, created_at__gte=since)
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(avg_rating=Avg('seller_rating'))
        .order_by('month')
    )

    overview = delivered_sales.aggregate(total_revenue=Sum('final_price'), total_orders=Count('id'))
    overall_avg_rating = Review.objects.filter(order__book__seller=user).aggregate(avg=Avg('seller_rating'))['avg']

    analytics_data = {
        'sales_labels': [row['month'].strftime('%b %Y') for row in sales_over_time],
        'sales_revenue': [float(row['revenue'] or 0) for row in sales_over_time],
        'sales_orders': [row['orders'] for row in sales_over_time],
        'category_labels': [row['book__category__name'] or 'Uncategorized' for row in best_categories],
        'category_revenue': [float(row['revenue'] or 0) for row in best_categories],
        'rating_labels': [row['month'].strftime('%b %Y') for row in rating_trend],
        'rating_values': [round(row['avg_rating'], 2) if row['avg_rating'] is not None else None for row in rating_trend],
    }

    context = {
        'analytics_data_json': json.dumps(analytics_data),
        'total_revenue': overview['total_revenue'] or 0,
        'total_delivered_orders': overview['total_orders'] or 0,
        'overall_avg_rating': round(overall_avg_rating, 1) if overall_avg_rating is not None else None,
        'best_categories': best_categories,
        'has_data': bool(sales_over_time or best_categories or rating_trend),
    }
    return render(request, 'accounts/analytics.html', context)


# --- Seller verification: Email OTP --------------------------------------

@login_required
def seller_verification(request):
    user = request.user
    context = {
        'otp_form': OTPForm(),
        'email_otp_sent': bool(user.email_otp and not user.email_verified),
    }
    return render(request, 'accounts/verify_seller.html', context)


@login_required
@require_POST
@rate_limit('otp-send-email', 5, 3600)
@rate_limit('otp-send-email-ip', 30, 3600, by='ip')
def send_email_otp(request):
    user = request.user
    if user.email_verified:
        messages.info(request, _('Your email is already verified.'))
    elif not User.can_resend_otp(user.email_otp_sent_at):
        messages.error(request, _('Please wait a bit before requesting another code.'))
    else:
        otp = generate_otp()
        user.set_otp('email_otp', otp)
        user.email_otp_sent_at = timezone.now()
        user.email_otp_attempts = 0
        user.save(update_fields=['email_otp', 'email_otp_sent_at', 'email_otp_attempts'])
        notify_email_otp(user, otp)
        messages.success(request, _('A verification code was sent to %(email)s.') % {'email': user.email})
    return redirect('accounts:verify_seller')


@login_required
@require_POST
@rate_limit('otp-confirm-email', 10, 3600)
@rate_limit('otp-confirm-email-ip', 30, 3600, by='ip')
def confirm_email_otp(request):
    form = OTPForm(request.POST)
    user = request.user
    if form.is_valid() and user.check_email_otp(form.cleaned_data['otp']):
        user.email_verified = True
        user.email_otp = ''
        update_fields = ['email_verified', 'email_otp']

        # Fraud Detection: duplicate accounts. Verification is the point the
        # account's identity signals (phone) are worth checking, so look for
        # another seller who looks like the same person - see
        # accounts.fraud_detection. Informational only; the account isn't
        # blocked, just flagged for admin review.
        linked = find_linked_accounts(user)
        if linked:
            user.is_duplicate_flagged = True
            user.duplicate_flag_reason = '; '.join(
                f"{l['user'].username} ({l['reason']})" for l in linked
            )
            update_fields += ['is_duplicate_flagged', 'duplicate_flag_reason']

        user.save(update_fields=update_fields)
        messages.success(request, _('Email verified!'))
    else:
        messages.error(request, _('Incorrect or expired code. Please request a new one.'))
    return redirect('accounts:verify_seller')
