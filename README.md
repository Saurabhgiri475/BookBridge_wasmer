# BookBridge — Buy & Sell Used Books Marketplace

A working Django marketplace for buying and selling used academic books (CBSE, ICSE,
JEE/NEET, UPSC, college/university, and general books), inspired by the OLX +
Amazon + BookScouter-style concept.

This is a **real, runnable application** — not a mockup. Every feature listed
below in "Fully working" actually functions end-to-end (verified by walking a
book through offer → checkout → escrow payment → shipping → OTP delivery →
review), including live payments through Razorpay and live courier
assignment/tracking through Shiprocket once you add your own account
credentials (see §1a — both have free/test tiers, no payment required to try
this out). A separate "Simulated / stubbed" section is listed honestly for
the pieces that still need other paid third-party accounts (SMS, cloud
storage) that only you can provision.

---

## 1. Quick start

**API keys (real integrations — see §1a below):** payments run through a real
Razorpay account and courier assignment/tracking runs through a real
Shiprocket account. Without keys configured, checkout and shipment creation
will show a clear inline error instead of silently faking anything — the
rest of the app still works.

**System dependency (one-time, OS-level — not pip):** the AI cover-scan
feature falls back to the Tesseract OCR engine, which pytesseract calls out
to, whenever the online engines aren't configured or fail (see below). Install it
once per machine:

- **Windows**: download and run the installer from
  https://github.com/UB-Mannheim/tesseract/wiki, then either add its install
  folder to your PATH, or point pytesseract at it by adding this near the top
  of `books/ocr_utils.py`:
  ```python
  pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
  ```
- **macOS**: `brew install tesseract`
- **Linux (Debian/Ubuntu)**: `sudo apt-get install tesseract-ocr`

If Tesseract isn't installed, every other feature in the app still works —
only the "Scan Cover" button on the Sell page will show an error (unless
Google Vision is configured, in which case Vision handles scanning instead).

**Cover-scan OCR engines (all optional, free keys).** `books/ocr_utils.py`
tries them in order and moves to the next whenever one is unconfigured, fails,
or reads nothing useful:

1. **OCR.space** — `OCR_SPACE_API_KEY`. Free key by email (no card):
   https://ocr.space/ocrapi/freekey (about 25,000 requests/month, 1 MB file
   limit — photos are compressed automatically).
2. **Google Gemini** — `GEMINI_API_KEY` (+ optional `GEMINI_MODEL`). Free key,
   no card: https://aistudio.google.com/apikey. Google sets and may change the
   free limits.
3. **Google Cloud Vision** — `GOOGLE_VISION_API_KEY`, only if you set it
   (needs billing enabled on the project; has a monthly free tier).
4. **Tesseract** — offline, no key, always the last resort.

Leave any key blank to skip that engine.

```bash
# 1. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Apply database migrations (SQLite, zero setup)
python manage.py migrate

# 4. Create an admin account
python manage.py createsuperuser

# 5. (Optional but recommended) Seed demo categories, sellers, and books
python manage.py seed_demo

# 6. Run the development server
python manage.py runserver
```

Visit **http://127.0.0.1:8000/** for the storefront and
**http://127.0.0.1:8000/admin/** for the admin dashboard.

### 1a. Configuring real payments (Razorpay) and courier tracking (Shiprocket)

Both are wired to the real APIs — no fake data is generated for either.

1. Copy the env template: `cp .env.example .env`
2. **Razorpay** — sign up at https://dashboard.razorpay.com, grab your
   **Test Mode** Key ID/Secret from Settings > API Keys, and put them in
   `.env` as `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`. Test mode lets you
   pay with fake card/UPI details that "succeed" without moving real money —
   see https://razorpay.com/docs/payments/payments/test-card-upi-details/.
   Optionally add a webhook (Settings > Webhooks) pointing at
   `https://<your-domain>/orders/webhook/razorpay/` with the `payment.captured`,
   `payment.failed`, and `refund.processed` events, and put its signing
   secret in `RAZORPAY_WEBHOOK_SECRET` — this is a server-to-server backstop
   in case a buyer closes the tab mid-payment.
3. **Shiprocket** — sign up at https://app.shiprocket.in (they have a free
   trial), add at least one **Pickup Location** under Settings > Pickup
   Locations, and put your dashboard login + that location's nickname in
   `.env` as `SHIPROCKET_EMAIL` / `SHIPROCKET_PASSWORD` /
   `SHIPROCKET_PICKUP_LOCATION`. On checkout, BookBridge creates a real
   Shiprocket order and asks it to assign whichever courier (Delhivery,
   Ekart, Xpressbees, DTDC, etc.) actually serves that pincode, and stores
   the real AWB (tracking) number it returns. The order detail page has a
   "Refresh Tracking" button that pulls live scan history from Shiprocket;
   you can also configure a webhook (Settings > API > Webhooks) pointing at
   `/orders/webhook/shiprocket/` with a shared token in
   `SHIPROCKET_WEBHOOK_TOKEN` for push updates instead of manual refresh.

If either isn't configured, the relevant action fails with a plain-language
error (e.g. "Razorpay isn't configured on this server yet") instead of
silently faking success — you'll know immediately if a key is missing.

### 1b. Configuring real email + SMS/WhatsApp/Push notifications

Also wired to real providers - nothing here is faked either.

1. **Email** — works out of the box with no setup: `EMAIL_BACKEND` defaults
   to Django's console backend, so every email just prints to your
   `runserver` terminal. For real delivery, set `EMAIL_BACKEND` to
   `django.core.mail.backends.smtp.EmailBackend` in `.env` plus
   `EMAIL_HOST`/`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` — Gmail SMTP works
   fine for testing (generate an "App Password" at
   https://myaccount.google.com/apppasswords, since Gmail blocks your normal
   login password over SMTP), or point it at SendGrid/Mailgun/SES's SMTP
   relay for production volume.
2. **SMS/WhatsApp** — sign up at https://console.twilio.com (free trial
   credit, no card required to start), put your Account SID/Auth Token in
   `.env` as `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`, and buy/verify a
   number for `TWILIO_SMS_FROM`. WhatsApp uses Twilio's free sandbox number
   by default (`TWILIO_WHATSAPP_FROM`) — the recipient has to WhatsApp
   "join &lt;your-sandbox-word&gt;" to that number once before Twilio will
   deliver to them; that's a Twilio sandbox limitation, not a bug here. If
   WhatsApp isn't reachable, delivery-OTP messages automatically fall back
   to plain SMS.
3. **Push Notifications** — real browser Web Push (VAPID), no Firebase/APNs
   account needed. Generate a keypair once with `pip install py-vapid &&
   vapid --gen`, put the public/private key in `.env` as
   `WEBPUSH_VAPID_PUBLIC_KEY`/`WEBPUSH_VAPID_PRIVATE_KEY`. A user turns it
   on themselves from **Notification Preferences** (bell icon → Preferences,
   or `/notifications/preferences/`) — that's what registers
   `static/js/notifications-sw.js` (via bookmarket/pwa.py) as the PWA service worker and calls
   `PushManager.subscribe()`. Leave the keys blank to keep push disabled;
   every other channel works fine without it.
4. **In-app Notification Center** — no setup needed, always on. Every event
   below lands at `/notifications/` (bell icon in the navbar) regardless of
   channel preferences, so there's always one place to see everything.

**Per-user channel preferences** (`/notifications/preferences/`,
`notifications.NotificationPreference`): email/push/in-app default **on**,
SMS/WhatsApp default **off** since each message costs money via Twilio.
`bookmarket/notify.py`'s `dispatch()` is the single fan-out point every
event below goes through — it creates the in-app Notification, then sends
email/push/SMS/WhatsApp for whichever channels that user has enabled.
Delivery OTPs and email verification codes bypass preferences
entirely (`notify_phone`/`send_email` called directly) since those aren't
optional — the buyer needs the OTP to receive their book regardless of
their notification settings.

What's actually wired up: signup welcome email (email only — sent before an
account, and its preferences, exist), new-offer/offer-response, order-placed
(to both buyer and seller), an order-status update on every seller-side
status change, a delivery-confirmed notice + seller payout notice, a
return-request notice to the seller, a refund-issued notice, new chat
messages (in-app + push only — SMS/WhatsApp would be excessive per message),
saved-search and book-request matches, and — the one that matters most for
trust — the delivery OTP itself is sent via WhatsApp/SMS the moment an order
goes "Out for Delivery" (sourced from a real Shiprocket courier update or
the seller marking it manually), instead of just being printed on the order
page. If Twilio isn't configured, the order page falls back to showing the
OTP inline with a note explaining why, so you can still test the flow
end-to-end.

If you ran `seed_demo`, you can log in immediately with:
- `demo_seller` / `demo12345` — has 8 listed books
- `demo_buyer` / `demo12345` — a buyer account

---

## 2. Fully working features

**Accounts**
- Sign up / log in / log out, buyer/seller/both roles
- Editable profile (address, city, avatar, phone)
- Seller & buyer star ratings computed from real review data
- Personal dashboard: listings, revenue, pending orders, purchase history

**Listings**
- Create/edit/remove book listings with up to 6 images (front/back/side/pages)
- Rich metadata: ISBN, edition, subject, board, university, branch, semester,
  condition, original vs. selling price (auto discount %)
- **AI book cover scanning (real OCR)** — on the "Sell a Book" page, upload a
  cover photo and click "Scan Cover" to auto-fill title, author, publisher,
  ISBN, and edition. Tries OCR.space, then Gemini, then (if configured) Google
  Cloud Vision, and finally offline Tesseract OCR — each falls through to the
  next on any failure. The same regex/keyword heuristics
  (`books/ocr_utils.py`) turn raw text into structured fields. It pre-fills the form for the seller to
  review/correct — it never auto-submits, since OCR-off-a-photo will
  occasionally get a field wrong on cluttered covers.
- "Market price estimate" — a simple, genuinely-computed average of other
  listings with the same title (a lightweight real substitute for the
  spec's "AI price prediction")
- **AI Fraud Detection** — runs automatically whenever a listing is
  published or edited (`books/fraud_detection.py`). Five rule-based
  checks, each genuinely computed (no external API/key needed):
  reused/stolen photos (Pillow perceptual-hash fingerprint compared
  against every other seller's images), descriptions copied from another
  seller's listing (`difflib` similarity), spam content (off-platform
  contact info, spam keyword stuffing, shouty caps), suspicious pricing
  (far below/above the market average for comparable listings), and
  duplicate seller accounts (`accounts/fraud_detection.py`, matched on
  phone number, checked when a seller verifies their email). Listings that score high-risk are held
  as `under_review` instead of going live to buyers immediately; an admin
  can approve them from the Django admin (`Book` list → "Approve selected
  listings held under review"). Everything below the threshold still
  publishes normally, with the flags kept on the listing for admin
  visibility.

**Search & discovery**
- Keyword + multi-filter search (category, board, university, semester,
  condition, city, price range)
- **Natural-language "AI Search"** — try typing *"I need DBMS book for B.Tech
  3rd semester AKTU"* into the AI search box on the homepage. This is a
  genuinely working rule-based parser (`books/nlp_search.py`) that extracts
  semester/university/board/branch from free text — no external API or key
  required. See §4 for how to upgrade it to a real LLM/NLP call.
- Wishlist, recently-viewed, and a same-category recommendation feed

**Book requests**
- Buyers can post "I need this book" requests
- Sellers get a "Requests that might match your books" page

**Digital Books**
- Sellers can list Digital Books (PDF Notes, Handwritten Notes, Question
  Papers, Lab Manuals) instead of a physical book - toggle "Digital Book /
  Material" on the Sell page, pick a format, and upload the file
  (`Book.listing_type`/`digital_type`/`digital_file`)
- Digital orders skip shipping entirely: no delivery address required, Cash
  on Delivery isn't offered, delivery charge is ₹0, and no Shiprocket
  shipment/AWB/OTP step happens
- On successful payment the order is marked delivered immediately, escrow
  releases right away, cashback/referral rewards fire the same as a
  confirmed physical delivery, and the buyer gets the file both emailed to
  them and available via a login-gated download button on the order page
  (`orders:download_digital` - checks the requester is the paying buyer
  before serving the file, so a leaked link alone can't pull it)
- Browse/homepage support filtering by listing type and digital format

**Negotiation**
- Buyer makes an offer → seller can accept / reject / counter-offer →
  accepted price flows automatically into checkout

**Orders & fulfillment**
- Checkout with delivery address + payment method selection
- On order placement, a **real Shiprocket shipment is created** and a
  courier (Delhivery/Ekart/Shadowfax/Xpressbees/DTDC/etc., whichever
  Shiprocket routes to that pincode) is assigned, returning a genuine AWB
  tracking number — plus a locally-generated 4-digit delivery OTP (the
  marketplace's own escrow-release checkpoint, separate from courier
  tracking)
- Seller-side status pipeline: Placed → Accepted → Shipped → Out for Delivery,
  with a "Refresh Tracking" button (and optional webhook) pulling live scan
  history from Shiprocket
- Buyer confirms delivery by entering the OTP, which **releases the escrowed
  payment** to the seller and unlocks reviewing/returns
- Order detail page shows a live visual tracking timeline plus real courier
  scan events

**Payments (real Razorpay integration)**
- Online payment methods (UPI/card/wallet) create a real Razorpay Order and
  hand off to Razorpay Checkout; a signed client-side callback *and* a
  server-side webhook both verify the payment before it's trusted
- `Payment.status` moves `created` → `escrow_held` → `released`, modelling
  marketplace escrow on top of a real captured Razorpay payment (Razorpay
  itself just captures/refunds — true fund-custody escrow needs a separate
  KYC'd product like Razorpay Route, so payout-to-seller is still your own
  process once `released`)
- Admin can approve a return and issue a **real Razorpay refund** in one
  action (COD orders, which never touched the gateway, are flagged for
  manual refund instead)
- COD stays `pending` → `released` with no gateway involved, since money
  changes hands physically at the door

**Wallet, Referrals, Cashback & Coupons**
- Every user has a **BookBridge Wallet** (`wallet/` app) with a running
  balance and a full credit/debit transaction ledger — never edited
  directly, always through `wallet/services.py`, so the balance can't drift
  from its history
- **Cashback**: 2% of an order's price is credited to the buyer's wallet the
  moment delivery is confirmed (`award_delivery_cashback()`)
- **Referrals**: every user gets a shareable referral code/link (wallet
  page); a friend who signs up via it gets an immediate ₹50 wallet credit,
  and the referrer gets ₹100 once that friend's *first* order is delivered
  (`reward_referral_if_eligible()` — guarded so a referrer is only ever
  paid once per friend)
- Wallet balance can be spent instantly at checkout ("BookBridge Wallet" as
  the payment method) instead of card/UPI/COD
- **Coupons** (`coupons/` app) — admin-created promo codes with percentage
  or flat discounts, an optional cap on percentage discounts, a minimum
  order value, an optional expiry window, per-user and total redemption
  limits, and a "first order only" flag. On the checkout page, entering a
  code and clicking "Apply" validates it live (`coupons/services.py`) and
  updates the order summary before the buyer places the order; the actual
  redemption is only recorded once the order is created, and is
  automatically released (via a cascading delete) if the order itself gets
  cancelled before payment completes — so a failed checkout never
  permanently "spends" a use of the coupon. Manage coupons from Django
  admin, or run `python manage.py seed_coupons` for a few ready-made
  samples (`WELCOME50`, `SAVE10`, `FEST20`) to test with locally. The
  discount always comes off what the buyer pays, never the seller's payout
  — same as cashback/referral, the platform funds it.

**Reviews & returns**
- Buyers rate seller, book quality, and delivery after a delivered order
- Return requests (wrong book / damaged / missing pages) with admin-reviewable status

**Messaging**
- Per-listing buyer↔seller chat: text, image attachments, and free-text
  location sharing

**Admin (Django admin, already wired up)**
- Manage users, listings, categories, orders, payments, shipments, reviews,
  returns
- Bulk actions: mark listings as removed (fraud/spam) or featured

---

## 3. Simulated / stubbed (needs your own API keys to go fully live)

These are implemented with realistic, working placeholder logic so the app
runs end-to-end today — but a production launch would swap them for real
integrations:

| Feature in the original spec        | What's here now                                   | To go live, add                          |
|--------------------------------------|----------------------------------------------------|-------------------------------------------|
| Real payment gateway (Razorpay)      | **Live** — real Razorpay Orders, Checkout, signature verification, webhook, refunds | Just add your live-mode keys once you've gone through Razorpay's KYC |
| Delivery partner APIs (Shiprocket)   | **Live** — real Shiprocket order/AWB/tracking, webhook support | Just add your Shiprocket credentials + pickup location |
| SMS/WhatsApp OTP & notifications      | **Live** — real transactional email + Twilio SMS/WhatsApp + Web Push + in-app Notification Center, see §1b | Just add your Twilio + SMTP + VAPID credentials |
| AI price prediction (ML)              | **Live** — real scikit-learn RandomForestRegressor trained from completed sales (see §4b); rule-based blend (condition/age/edition/demand/similar-listing average) as fallback for books it hasn't learned from yet | Run `python manage.py train_price_model` once you have some sales history |
| AI natural-language search            | **Live** — real Groq LLM call (free tier), offline rule-based parser as fallback (see §4) | Just add your `GROQ_API_KEY`              |
| Fake image / duplicate detection      | **Live** — perceptual-hash image reuse detection + copied-description/spam/price/duplicate-account checks (see below) | Reverse image search against the open web (TinEye/Google Vision) for images stolen from *outside* the platform |
| Recommendation engine (collaborative) | **Live** — real scikit-learn item-item collaborative filtering, trained from Order history (see §4a); real-time SQL query as fallback for un-trained books | Run `python manage.py train_recommendations` once you have some order history |
| Cloud image storage (S3/Cloudinary)   | Local `media/` folder                              | `django-storages` + S3/Cloudinary bucket  |
| Elasticsearch full-text search        | Django ORM `icontains` filtering                   | Elasticsearch/OpenSearch + django-haystack|

None of these are hard to add later — the models and views are already
structured so each one is a contained swap. Every third-party integration
lives behind a single module (`orders/shiprocket.py`, `orders/payments.py`,
`books/vision_ocr.py`, `bookmarket/notify.py`), so swapping a provider means
rewriting that module and nothing else.

---

## 4. "AI search": real LLM parsing, with an offline fallback

`books/nlp_search.py` contains `parse_query()`, a small, dependency-free
function that recognizes semesters, known universities, boards, branches, and
exam names from a free-text query using regex/keyword matching, and strips
them out to leave a clean keyword string. This always works, needs no API
key, and is what runs by default.

Set `GROQ_API_KEY` in `.env` (see `.env.example`) to upgrade to a real LLM
call instead: `books/llm_search.py` sends the query to the Groq API (free
tier, no credit card) and asks for the same dict shape back, so it copes with
phrasing, universities, and branches the regex parser was never taught.
`books/search_ai.py` is the
single entry point both `/browse/?ai=1&q=...` and the homepage search box call
- it tries the LLM first when a key is configured and transparently falls
back to the offline parser on any failure (missing key, network error, rate
limit), so search never breaks because of an API outage. Leave the key blank
to keep using the free offline parser only - no code changes needed either
way.

## 4a. "Also bought" recommendations: real trained collaborative filtering

`books/recommendations.py`'s `also_bought()` (shown on every book's detail
page) tries a real trained model first: `books/ml_recommendations.py` builds
a buyer x book interaction matrix from `Order` history and computes
book-to-book cosine similarity with scikit-learn - two books are "similar"
when they tend to be bought by the same people, the standard item-based
collaborative filtering approach.

This needs training data to be worth anything, so it's a batch step rather
than something computed on every page load:

```
python manage.py train_recommendations
```

Run it once you have a reasonable batch of orders (locally, `seed_demo`
alone won't create enough repeat-buyer history to train on - place a few
orders as different demo buyers first, or just try it and read the message).
Re-run it periodically as more orders come in (a cron job/scheduled task
is the natural way to schedule this in production) - it's not
automatic, since retraining on every request would be wasteful and order
history doesn't change fast enough to need it.

If no model has been trained yet, or a given book isn't covered by the last
training run (listed or first sold since), `also_bought()` falls back
automatically to a real-time SQL query over `Order` history (cruder - raw
buyer-overlap count instead of proper cosine similarity - but needs no
training step and is never stale). Either path is genuine purchase-history
collaborative filtering, not a popularity or category heuristic.

## 4b. AI price prediction: real trained regression model

`books/price_recommendation.py`'s `recommend_price()` (shown on the Sell
page as the seller fills in the listing form) blends up to six signals into
a Market Price + Recommended Price range: condition, age, edition, demand
(wishlists/buy-requests/competing listings), previous sales of this/similar
books, and - when one has been trained - a real ML price model.

`books/ml_price_model.py` trains a scikit-learn `RandomForestRegressor` on
every completed sale's condition, category, age, and original listed price,
with the actual sale price (`Order.final_price`) as the target. Like the
recommendation engine above, this needs real sales history to be worth
anything, so it's a batch step:

```
python manage.py train_price_model
```

It needs at least 30 completed sales to train (reports plainly and leaves
any existing model untouched if there isn't enough yet - `seed_demo` alone
comfortably clears this locally). Re-run it periodically as more orders come
in, same as `train_recommendations`.

If no model has been trained yet, or a prediction fails for a particular
book, the "ML Price Model" signal is simply left out and the other five
rule-based signals still produce a full recommendation - nothing on the
Sell page depends on this model existing.

---

## 5. Project structure

```
bookmarket/
├── accounts/       # custom User model, auth, profile, dashboard, seller verification
├── books/          # listings, categories, search, wishlist, requests, OCR, fraud scan
├── orders/         # offers, checkout, payments, shipments, reviews, returns, invoices
├── wallet/         # balance + transaction ledger, cashback, referrals
├── coupons/        # promo codes, validation, redemption
├── notifications/  # in-app notification centre, per-user channel preferences
├── chatapp/        # buyer <-> seller messaging
├── adminpanel/     # staff moderation dashboard + audit log
├── templates/      # Bootstrap 5 templates for every page
├── static/
├── bookmarket/     # project settings, urls, notification fan-out
└── manage.py
```

## 6. Revenue-model hooks already in the data model

`Order.platform_commission_percent` (default 10%) and the computed
`Order.commission_amount` / `Order.seller_payout` properties give you a
working commission-based revenue model out of the box. Premium-seller
(`User.is_premium_seller`, `premium_until`) and featured-listing
(`Book.is_featured`) flags are already in the schema — you'd just need to
add a checkout flow for those tiers.

## 7. Known limitations for a real launch

- Defaults to SQLite for zero-setup local dev. Set `DATABASE_URL` (or the
  `DB_*` variables) and it uses PostgreSQL instead — see `DATABASES` in
  `bookmarket/settings.py`.
- `DEBUG` now defaults to **off** (fail closed). For local development put `DEBUG=True` in
  your `.env` (see `.env.example`); production needs a real `SECRET_KEY`, `ALLOWED_HOSTS`
  and a shared cache (`REDIS_URL`, or `CACHE_BACKEND=db`) - the app refuses to start otherwise.
- No rate limiting or CAPTCHA yet. Automated fraud/spam detection *is*
  implemented (see §2) — what's missing is throttling on the endpoints
  themselves, so a script can still hammer signup/login/search.
- Online (Razorpay) checkout now holds the listing for the paying buyer for `PAYMENT_HOLD_MINUTES`
  (default 20), so two buyers cannot both pay for one copy. The hold expires by itself; if a buyer pays
  after their hold lapsed and someone else already took the book, it is still logged for a manual refund.
- Email defaults to Django's console backend (prints to your terminal) until
  you set `EMAIL_BACKEND`/`EMAIL_HOST_*`; SMS/WhatsApp are no-ops until you
  set `TWILIO_*` — see §1b.



---

## Signup hardening & partner onboarding (new)

**Signup form** - live username/email availability (`/accounts/signup/check/`), password strength meter and
show/hide toggle (`static/js/auth-ui.js`; the meter is advisory, Django's validators still decide).

**Abuse protection** (`accounts/throttle.py`) - per-IP limits on signup attempts and on accounts created, a per-address
limit on verification emails, a honeypot field + too-fast-submit check, and an optional Cloudflare Turnstile captcha.
All limits are env-tunable: `SIGNUP_MAX_ATTEMPTS_PER_IP_PER_HOUR` (30), `SIGNUP_MAX_ACCOUNTS_PER_IP_PER_HOUR` (5),
`SIGNUP_MAX_PER_EMAIL_PER_DAY` (3), `AVAILABILITY_CHECKS_PER_IP_PER_MINUTE` (40).
Set `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` to switch the captcha on (blank = off).

> **Production:** counters live in Django's cache. With the default per-process cache each gunicorn worker counts
> separately, so set `REDIS_URL` (cache backend is switched automatically) or `CACHE_BACKEND=db` and run
> `python manage.py createcachetable`. Behind a reverse proxy also set `NUM_PROXIES` (e.g. `1`), otherwise every
> visitor looks like the proxy's IP.

**Partner application** - GSTIN format + state + checksum validation, PAN as an alternative to Aadhaar (only a hash and
the last 4 characters are stored, like Aadhaar), photo previews with format/size guidance, HEIC (iPhone) uploads converted
to JPEG (needs `pillow-heif`, in requirements.txt), EXIF/GPS stripped from proof photos, and applicants can edit details
or replace documents while the application is still pending (`/partners/apply/?edit=1`). Old files are deleted on replacement.
DigiLocker is not implemented - it needs API Setu/DigiLocker partner credentials.

After pulling these changes run `pip install -r requirements.txt` and `python manage.py migrate` (adds `partners.0006`).


---

## Lead auto-reassign & partner ratings/badges (new)

**Lead routing** (`partners/assignment.py`) - a new buyback lead is reserved for the nearest eligible partner for
`LEAD_RESPONSE_MINUTES` (default 120). "Responding" = making an offer or pressing **Pass**. If the window runs out - or the
partner pauses/is suspended meanwhile - it moves to the next-nearest partner, up to `LEAD_MAX_ASSIGNMENT_ROUNDS` (default 3),
then opens to every partner in range as it did before. Other partners can't see or bid on a reserved lead. A seller declining an
offer restarts routing without that partner. Partners are notified when a lead is reserved for them and when it lapses.

> **Schedule it:** run `python manage.py reassign_stale_leads` every 5-10 minutes (cron / systemd timer / host scheduler).
> Opening the partner dashboard also sweeps expired leads, so nothing gets stuck if you forget - but a lead only moves on when
> *someone* triggers a sweep, so the scheduler is what makes the timing reliable.
> Not covered: a seller-accepted offer where the partner never shows up for pickup - the seller chose that partner, so it is not
> reassigned automatically.

**Ratings & badges** (`partners/badges.py`) - after a completed buyback the seller can rate the partner 1-5 with an optional comment
(one review per buyback, only by that seller, not editable). Staff can hide a review in the admin (hidden reviews don't count).
Badges are computed live from real data, never stored: First Pickup / Active Buyer (10) / Pro Partner (25) by completed buybacks,
Quick Responder (median offer time <= 60 min over 5+ offers), Trusted Partner (4.5+ from 5+ reviews), Top Rated (4.8+ from 15+).
Shown on the partner dashboard, on offers sellers receive, and on a public partner page (`/partners/shop/<id>/` - shop name, city,
rating, reviews with masked reviewer names; no contact details or documents). Response time is only measured for offers made after
this update (`offered_at`), so Quick Responder starts empty for existing partners.

Run `python manage.py migrate` (adds `partners.0007`).

## Private file storage (ID proofs, shop proofs, paid digital files, chat photos, buyback photos)

Sensitive uploads are saved under `PRIVATE_MEDIA_ROOT` (default `private_media/`), **not** under the public `media/` folder, and have no URL.

- Partner ID / shop proofs are opened only through `partners:proof_file` - allowed for the applicant and staff, 404 for everyone else. Staff access is written to the audit log (`view_partner_proof`).
- Paid digital files are served only through the existing buyer-checked download view.
- Chat photos are opened only through `chatapp:image` (the two people in the conversation; 404 for everyone else, staff included).
- Buyback photos are opened only through `partners:buyback_image` (the seller, an approved partner who may see that lead, and 2FA-verified staff).
- Deploying: never map `private_media/` in nginx / your static or media config, keep it out of git and broadly shared backups, and restrict the folder to the app's user.
- Upgrading an existing install: run `python manage.py move_private_files --dry-run`, then without `--dry-run`. Treat any older copies of the proofs (old zips, backups, a public `media/` folder) as exposed and delete them.
- Moving to S3/R2 later: change `get_private_storage()` in `bookmarket/storage.py` to a private-bucket backend; the authorised views stay the same.

## Content-Security-Policy (CSP)

A CSP is sent on every response, in **enforce** mode by default when `DEBUG` is off (`report-only` in local development, or set `CSP_MODE=report-only`): in report-only nothing is blocked, but violations are POSTed to `/csp-report/` and logged under the `bookmarket.csp` logger.

- Inline `<script>` blocks must carry `nonce="{{ csp_nonce }}"`. Inline `onclick/onsubmit/onchange` handlers are not allowed - use `data-confirm="..."`, `data-autosubmit`, `data-submit-form="id"` (see `static/js/ui-actions.js`).
- Anything from the server or a user that goes into an `innerHTML` template must be wrapped in `bbEsc(...)` (`static/js/bb-safe.js`), or use `textContent`.
- Rollout: run on the real site in report-only, watch the logs for "CSP violation", add any legitimately needed host to `_CSP_DIRECTIVES` in `bookmarket/settings.py`, then set `CSP_MODE=enforce`.
- Tests in `bookmarket/test_csp_and_xss.py` fail if an inline handler, a nonce-less inline script or an unescaped `${data.x}` interpolation is added.

## Verification codes (OTP) storage

Email verification codes are never stored in the database - only a keyed hash (`accounts/otp.py`: HMAC-SHA256 keyed from `SECRET_KEY`, bound to the user and the field). A leaked database cannot be turned back into codes without `SECRET_KEY`, and a hash copied between accounts doesn't validate. Codes still expire after 10 minutes, lock after 5 wrong tries, have a resend cooldown, and are rate-limited per account and per IP.

- Rotating `SECRET_KEY` invalidates any code that is still outstanding; users simply request a new one.
- The on-screen demo code (`DEMO_MODE` + `DEBUG` only) now lives in the user's own session, not the database.
- Not covered: the buyer's **delivery OTP** and the seller's **pickup OTP** are stored as-is, because the app has to show or re-send them later. They're only accepted from the logged-in buyer/partner and lock after 5 wrong tries.


## Book donation with certificate (new)

Users can donate books to NGOs / schools / libraries and receive a numbered, verifiable PDF certificate.
Everything lives in the `donations` app (mounted at `/donate/`).

**Flow:** donor submits a donation (pick organisation, list books, drop-off or pickup) -> staff **accept** it ->
books are handed over -> staff enter the number of books **actually received** and the certificate is issued for
that verified number (never the number the donor typed) -> donor is notified and gets the PDF by email.

**Set-up (one time):** in Django admin (`/admin/` -> *Donation partners*) add the organisations that accept books.
Until at least one is active, `/donate/new/` tells donors none are available yet. Organisations are managed by you -
no organisation data is bundled. (`python manage.py seed_demo_donation_partners` adds two clearly-labelled DEMO
entries for local testing only.)

**Pages:** `/donate/` (public landing + organisations), `/donate/new/`, `/donate/mine/`, `/donate/<id>/`,
`/donate/verify/<code>/` (public certificate check, also what the QR code on the PDF opens), and the staff queue at
`/donate/staff/` (linked from the Staff Dashboard, which also counts pending donations under "Need Your Attention").

**Certificates:**
- Code format `BBD-XXXXXXXXXX` (random, not sequential, so they can't be enumerated). Name / organisation / count are
  snapshotted at issue time.
- PDF is generated on demand (reportlab, landscape A4, QR code) and only the donor or staff can download it.
- The public verification page shows the name printed on the certificate, the organisation, count and date - nothing
  else (no email/phone/address). The donation form tells donors this.
- Staff can **revoke** a certificate (reason required): the page then shows "revoked", the PDF can no longer be downloaded,
  and it stops counting toward Impact.

**Impact integration:** only issued, non-revoked certificates count. Donated books are added to *Books Reused*
(`completed orders + donated books`) on the Impact page, the home page and each donor's profile (they also count
toward the existing badges). The Impact page has a new "Book Donations" section and an updated methodology note.

**Names in other scripts:** the PDF uses the built-in Helvetica font, so the form only accepts names it can print
(Latin letters incl. accents). To allow e.g. Devanagari names set `CERTIFICATE_FONT_PATH` in `.env` to a `.ttf` that
has those glyphs (such as Noto Sans Devanagari).

**Abuse limits:** 10 donation submissions per user per hour; certificate lookups limited per IP; total 500 books and
20 title rows per donation. Every staff action (accept / reject / issue / revoke) is written to the audit log.

## Seller payouts, UPI tracking & commission statements (new)

The `payouts/` app records what BookBridge owes each seller, tracks the UPI
transfers that settle it, and produces downloadable commission statements.

**How money actually moves:** BookBridge does *not* send payouts itself (that
needs a RazorpayX/bank payout API and business KYC). Staff send the UPI transfer
from their own UPI app, then record the UTR on **Staff -> Seller Payouts**
(`/payouts/staff/`). The app is the ledger and audit trail, not the payment rail.

- **A `Payout` row is created** in the same transaction that releases an order's
  payment (`orders/views.py`: `_release_on_delivery` and `_deliver_digital_order`,
  via `payouts.services.ensure_payout`). Amounts (sale price, commission %,
  commission, net) are snapshotted then, so a later rate change never rewrites a
  statement a seller has already seen.
- **Sellers** (`/payouts/`): add a UPI ID, see pending/paid payouts with UTRs, and
  download a commission statement as **PDF or CSV** for any of the last 12 months
  or the current/previous financial year (Apr-Mar). Periods are cut at midnight
  IST. Changing the UPI ID emails the seller (masked) - it is the first thing an
  account-takeover would change.
- **UPI name check (replaces Aadhaar verification).** Before the *first* payout to
  a UPI ID, staff open their UPI app, enter the ID, and type the account-holder
  name it shows on the payout page. `payouts/namecheck.py` compares it with the
  seller's account name (case, word order, titles and initials are ignored; two
  different people don't match). A mismatch is refused unless staff tick "Accept
  anyway" and give a reason - that is written to the audit log. `mark_paid` refuses
  until the check is done; later payouts to the same UPI ID need nothing, and
  changing the UPI ID starts the check again. Sellers need their full name on
  their profile.
- **Staff** (`/payouts/staff/`): queue with a pay-link that opens the UPI app with
  the amount filled in, one-UTR-covers-several-orders batch marking (one seller per
  transfer), hold / release / cancel with a required reason, a "UPI changed in the
  last 72h" warning, and a CSV export for reconciliation. Every action is written
  to the existing `AuditLog`.
- **Refunds**: refunding a return (Django admin actions in `orders/admin.py`)
  cancels an unpaid payout, or flags an already-paid one as "refunded after
  payout" so staff can recover the money. Recovery itself is manual.
- **Shopkeeper partners** get the same PDF/CSV for the buyback commission they
  owe BookBridge, from **Partner -> Earnings**.
- **Existing orders:** run `python manage.py backfill_payouts --dry-run`, then
  either `backfill_payouts` (staff review them as pending) or
  `backfill_payouts --mark-paid` if those sellers were already paid outside the
  app. Run it once, after `migrate`.

Statements are not GST tax invoices. If BookBridge is GST-registered and must
invoice sellers for commission, that needs a separate document with GSTIN/HSN/SAC
and sequential invoice numbers - decide that with your accountant first.


## Buyer dispute center (new)

The `disputes/` app is for everything the quick **Request Return** form can't
express: an item that never arrived, a partial refund, a back-and-forth with the
seller, photo evidence. Return requests still work exactly as before.

**Flow**

1. **Buyer opens a dispute** from the order page (**Report a problem**) - category,
   description, wanted outcome (full / partial refund with an amount, replacement),
   up to 5 photos. Allowed once an order has shipped: "not received" after
   `NOT_RECEIVED_AFTER_DAYS` (7) in transit, everything else within
   `DISPUTE_WINDOW_DAYS` (30) of delivery. One dispute per order (a withdrawn one can
   be redone). Digital orders only get the digital-relevant categories.
2. **Seller replies** (`/disputes/`) within `SELLER_RESPONSE_DAYS` (3), or clicks
   **I agree - refund the buyer**, which sends it straight to staff.
3. **Escalation**: the buyer can escalate once the seller has replied or the window
   has passed. Silent sellers are auto-escalated by
   `python manage.py escalate_overdue_disputes` - schedule it hourly (cron / your host's
   scheduler), like `reassign_stale_leads`.
4. **Staff decide** at **Staff -> Disputes** (`/disputes/staff/`, also a card on the staff
   dashboard): read the thread and photos, message both sides, leave staff-only notes,
   then resolve as *refund* (amount up to what the buyer paid, to the wallet or back to
   the original Razorpay payment), *replacement*, or *declined*. Every resolution needs
   an explanation both parties see and is written to the `AuditLog`.

**Money rules** (all in `disputes/services.py`; nothing else moves dispute money)

- A refund is only issued for an order the buyer actually paid for (so a COD order
  that was never delivered can be declined/replaced, not refunded), never twice, and
  never above `Order.total_amount`. The old Return Request refund actions in Django
  admin now skip an order that a dispute already refunded, and vice versa.
- Refunding at least the book price reverses the sale: payment -> refunded, the seller's
  payout is cancelled (or flagged "refunded after payout" if already paid) and any open
  return request is marked refunded.
- A smaller refund is partial: staff tick whether the **seller bears it** (deducted from
  a pending payout; flagged to recover if already paid) or BookBridge does. Unticking
  leaves the payout alone and just notes it. Change this default if your policy differs.
- While a dispute is active the seller's pending payout is **on hold**; it is released on
  withdrawal or resolution, but only if the dispute placed that hold (a hold staff set
  for another reason is never lifted). A payout created *after* a pre-delivery dispute
  starts on hold too.
- A Razorpay refund that fails changes nothing - the dispute stays open to retry.

**Photos** are private files (`bookmarket/storage.py`): re-encoded (EXIF/GPS stripped,
non-images rejected) and readable only by the buyer, the seller and staff.

**Upgrading:** `python manage.py migrate`. This adds `Order.delivered_at` (the dispute
window counts from it; existing delivered orders are back-filled from their last
update) plus the new `disputes` tables and an audit-log action.


## Password reset, support contact & footer (new)

- **Password reset by email**: *Forgot your password?* on the login page ->
  `/accounts/password-reset/`. The user enters their email; we send a link that works
  **once and for 1 hour** (`PASSWORD_RESET_TIMEOUT`). The reply is the same whether or not
  the address has an account, and emails are capped per address (3/hour) and per network
  (10/hour) - see `PASSWORD_RESET_MAX_PER_EMAIL_PER_HOUR` / `..._PER_IP_PER_HOUR`.
  Unverified signups get no link (they verify first). Choosing a new password uses the same
  validators and show/hide + strength helpers as signup, lifts any login lockout, signs out
  every other session, and emails the owner "your password was changed" (with the support
  contact in case it wasn't them).
- **Emails only really send once SMTP is configured.** Out of the box
  (`EMAIL_BACKEND` unset) they print to the `runserver` terminal - that's where you'll find
  the reset link while developing. For real delivery set the `EMAIL_*` values in `.env`
  (see `.env.example`; Gmail needs an App Password).
- **Support contact**: `SUPPORT_PHONE` and `SUPPORT_EMAIL` (defaults: 7985486802 and
  saurabhgiri95893@gmail.com) feed the footer, a new **/contact/** page, the login and
  password-reset pages and the security emails. Change them in `.env`, not in templates.
- **Footer**: two equal halves (brand + tagline + categories on the left; "Need help?" contact + links flush against the right edge on desktop),
  with the copyright centred underneath; stacks on phones.

## About page redesign (new)

`/about/` now has two illustrations (`static/img/about/hero.svg`, `mission.svg` - plain SVG, no
image downloads, so they stay sharp and light), live number cards, a two-lane "How it works",
feature and trust tiles, subject pills, an impact band, an FAQ and a closing call to action.
Every number is a real count from the database; the dispute windows quoted in the FAQ are read
from `disputes/services.py`, so they can't drift from the actual rules. To swap in real photos
later, replace the two SVGs (keep the `alt` text accurate).

## Staff security: two-factor login, roles, audit log

**Two-factor (TOTP).** Every staff account must enrol an authenticator app and enter a code once per session (12 hours) before it can use the Staff Dashboard, the payouts/disputes/donations staff pages, partner ID documents or the Django admin. Enrolment shows a QR code and 10 single-use recovery codes. Codes can't be replayed, wrong guesses are rate-limited, and the secret is stored encrypted with a key derived from `SECRET_KEY` (rotating it means staff re-enrol; use `python manage.py reset_staff_2fa <username>` for a lost device). Set `STAFF_2FA_REQUIRED=False` only for local development.

**Roles.** Run `python manage.py setup_staff_roles` to create the groups: Super Admin, Finance, Fraud Team, Customer Support, Partner Verification, Content Moderator. Each carries only the `cap_*` capabilities it needs; give staff a role in Django admin instead of making them superusers. Staff without the capability get a 403.

**Idempotency.** Wallet credits/debits take an `idempotency_key` (unique), so a retry or double-click can't pay or charge twice. Refunds go through one `RefundRecord` per order, claimed under a row lock, so an order can't be refunded twice by two paths (admin, dispute, return).

**Audit log.** Staff actions record who, what, when, the client IP and browser, the before/after values and the reason. Entries are append-only in the app (no edit or delete via the model, queryset or Django admin). For tamper-proofing against direct database access, also give the app's database user no UPDATE/DELETE rights on `adminpanel_auditlog`.


## Money-safety rules and payout hold

- **Prices:** a listing or offer must be at least ₹1 (`books.models.MIN_PRICE`). Enforced by model validators, DB check constraints (`book_price_gte_one`, `offer_amount_gte_one`, ...), the bulk importer, the checkout (refuses an order whose price is below the minimum or whose total after a coupon is not positive) and `wallet.services.credit/debit`, which now raise `InvalidAmount` for amounts <= 0 instead of silently doing nothing. Migrations `books.0012` and `orders.0010` first repair old bad rows (a listing under ₹1 is set to ₹1 and moved to `under_review` for a human to re-price).
- **UPI change hold:** changing a payout UPI ID requires the account password, is rate limited and audit-logged, and starts a 72-hour hold (`PAYOUT_UPI_HOLD_HOURS`) during which `payouts.services.mark_paid` refuses to pay that seller. Staff with finance rights can waive the hold for the UPI ID currently on file with a mandatory written reason ("Override hold" on the payout page, audit action `override_upi_hold`); the waiver ends at the next UPI change.
- Upgrading: `python manage.py migrate`, then `python manage.py move_private_files --dry-run` and again without `--dry-run` to move existing chat and buyback photos out of public `media/`. Also block `/media/chat_images/` and `/media/buyback_requests/` at the web server and delete old copies from backups.

## Upload checks, referral limits (hardening pass)

- **Digital files** (`books/file_checks.py`): the real file contents must match the extension (PDF/PNG/JPEG/Word signatures); PDFs with JavaScript or launch actions are rejected; ZIP/DOCX files must be intact, free of path traversal, program files, password protection, macros and zip-bomb behaviour. For an extra layer, scan new uploads with ClamAV in the same place.
- **Referrals** (`wallet/services.py`): the reward needs both emails verified, a referred order of at least `REFERRAL_MIN_ORDER_VALUE` (₹200), no self-referral or same-phone accounts, and is capped per referrer (3/day, 10/30 days). Welcome bonuses are capped at 5/day per referrer link and blocked for linked accounts.
- New env vars: `ALLOW_LOCMEM_CACHE`, `PAYMENT_HOLD_MINUTES`, `PAYOUT_UPI_HOLD_HOURS`.
