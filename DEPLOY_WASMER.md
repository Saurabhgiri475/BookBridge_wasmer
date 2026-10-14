# Deploying BookBridge on Wasmer Edge

Wasmer detects Django from `requirements.txt` + `manage.py`, installs the packages, runs
`migrate`, and serves `bookmarket.asgi` with Uvicorn. This project has been prepared for that:

* `requirements.txt` has `uvicorn` (no gunicorn) and no native-only extras. `scikit-learn`, `scipy`,
  `numpy`, `pandas`, `joblib` and `redis` are also in `requirements-optional.txt`: together they made the
  build exceed Wasmer's 1536 MB builder memory ("Build used too much memory"). The ML price model and
  ML recommendations switch themselves off when these are missing and the app uses its rule-based / SQL fallbacks.
  `pillow-heif`
  (iPhone HEIC photos) and `pywebpush` (browser push notifications) moved to `requirements-optional.txt`:
  Wasmer only installs prebuilt packages and these (or their dependencies) have none. Without them `.heic`
  uploads are politely refused and browser push is skipped; email, SMS/WhatsApp and in-app notifications still work.
* Static files work with or without `collectstatic` (WhiteNoise falls back to the apps' static folders).
* A migration (`accounts/0010_create_cache_table`) creates the shared cache table
  automatically when `CACHE_BACKEND=db`, so you do not need to run `createcachetable`.
* The Postgres `DB_HOST / DB_PORT / DB_NAME / DB_USERNAME / DB_PASSWORD` variables that Wasmer fills in
  are read directly. `DATABASE_URL` also works (any external Postgres).

## Steps
1. Push this folder to a Git repo (or upload it) and create the app at https://wasmer.io/new.
2. Choose **PostgreSQL** as the database. Wasmer then provides the `DB_*` variables itself.
3. Set the environment variables below (copy from `.env.wasmer.example`).
4. Deploy. After the first deploy, create the admin user and open `https://<your-app>.wasmer.app/`.

## Required environment variables
| Variable | Value |
|---|---|
| `SECRET_KEY` | a long random string (`python -c "import secrets;print(secrets.token_urlsafe(60))"`) |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | `<your-app>.wasmer.app` (add your own domain later, comma separated) |
| `CSRF_TRUSTED_ORIGINS` | `https://<your-app>.wasmer.app` |
| `SITE_URL` | `https://<your-app>.wasmer.app` |
| `CACHE_BACKEND` | `db` |
| `DB_SSLMODE` | `require` only if the database refuses non-TLS connections |

Recommended: `NUM_PROXIES=1` (so rate limits see each visitor's real IP instead of Wasmer's proxy;
check Wasmer's docs that its edge sets `X-Forwarded-For`), `DEMO_MODE=False`.
Add `EMAIL_*`, `RAZORPAY_*`, `GEMINI_API_KEY` / `OCR_SPACE_API_KEY` etc. when you enable those features.

## Sending email (signup codes, password reset)
Set `EMAIL_BACKEND=bookmarket.email_backend.CertifiSMTPEmailBackend` plus `EMAIL_HOST`, `EMAIL_PORT`,
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS=True`, `DEFAULT_FROM_EMAIL`. Do not use Django's stock
SMTP backend on Wasmer: its Python has no system CA certificates, so TLS fails with
`CERTIFICATE_VERIFY_FAILED`. The custom backend uses certifi's CA bundle and keeps verification on.
For Gmail use an App Password (needs 2-step verification), not your normal password.

## Uploaded files (important) - use a cloud bucket
Wasmer's local disk is read-only / wiped on redeploy, so profile photos and book photos fail with
"Internal Server Error" unless they go to an S3-compatible bucket. Any provider works (Backblaze B2, Cloudflare R2,
Supabase Storage, AWS S3...). You need **two buckets**: one public (book photos, avatars) and one private
(ID proofs, paid digital books, chat and dispute files). The "public" one can stay private too (signed links).

### Backblaze B2 (10 GB free, no card needed with private buckets)
1. Sign up at backblaze.com > B2 Cloud Storage.
2. Buckets > Create a Bucket, **twice**, both with files **Private** (public buckets make Backblaze ask for a card or $1):
   `bookbridge-media` and `bookbridge-private` (bucket names are global, so add a suffix if taken).
3. The bucket page shows its **Endpoint** (like `s3.us-west-004.backblazeb2.com`); the region is the middle part (`us-west-004`).
4. App Keys > Add a New Application Key: allow access to **All** buckets, **Read and Write**. Copy the `keyID` and
   `applicationKey` (shown once).

| Variable | Value |
|---|---|
| `S3_ENDPOINT_URL` | `https://s3.us-west-004.backblazeb2.com` (use yours) |
| `S3_REGION` | `us-west-004` (use yours) |
| `S3_ACCESS_KEY_ID` | the `keyID` |
| `S3_SECRET_ACCESS_KEY` | the `applicationKey` |
| `S3_PUBLIC_BUCKET` | `bookbridge-media` |
| `S3_PRIVATE_BUCKET` | `bookbridge-private` |

Leave `S3_PUBLIC_DOMAIN` **unset**: book photos and avatars are then served through signed links that expire after
7 days (the same link is reused for ~6 days so browsers can cache the image). If you later make the media bucket
public, set `S3_PUBLIC_DOMAIN` to its public domain (for B2: `f004.backblazeb2.com/file/bookbridge-media`) to get
permanent plain URLs instead.

### Cloudflare R2
Same variables: endpoint `https://<account-id>.r2.cloudflarestorage.com`, region `auto`, and `S3_PUBLIC_DOMAIN` is the
public bucket's `pub-xxxx.r2.dev` (or custom) domain.

Photos load straight from the storage provider. Private files stay in the private bucket and are only streamed through
BookBridge's permission-checked views. Files uploaded before the switch lived on the old disk and are not copied over.
If your provider needs path-style URLs set `S3_ADDRESSING_STYLE=path`.

Without buckets, uploads use `MEDIA_ROOT` / `PRIVATE_MEDIA_ROOT` on local disk; on Wasmer that needs a
persistent volume (e.g. `MEDIA_ROOT=/data/media`, `PRIVATE_MEDIA_ROOT=/data/private_media`, the latter
not inside the former).

## Create the admin (superuser) - no shell needed
Wasmer's drag-and-drop deploy has no terminal, so BookBridge creates the first admin during the deploy's `migrate` step
from three environment variables:

| Variable | Value |
|---|---|
| `DJANGO_SUPERUSER_USERNAME` | e.g. `admin` |
| `DJANGO_SUPERUSER_EMAIL` | your email |
| `DJANGO_SUPERUSER_PASSWORD` | a strong password (Django's password rules apply: 8+ characters, not common or all-numeric) |

Redeploy, then log in at `https://<your-app>.wasmer.app/admin/`. **Afterwards delete the three variables** (the password
should not stay in your settings) and redeploy. It only creates a missing account: it never changes an existing user or
password. Anyone could have signed up with that username/email first, in which case nothing happens - use another one.
On a machine with a terminal (local, SSH) the usual `python manage.py createsuperuser` works too.

### Staff roles (instead of more superusers)
Each deploy also creates the staff role groups (Super Admin, Finance, Fraud Team, Customer Support, Partner
Verification, Content Moderator) if they are missing - roles you have already tuned are never overwritten. (If your roles
were created by an older deploy and look incomplete, e.g. Finance cannot open payouts, set `RESET_STAFF_ROLES=1` for one
deploy to restore the defaults, then remove it. This overwrites any tuning.) Keep **one**
superuser (the owner, created above). Then give everyone else a role: Django admin > Users > open the person > tick
**Staff status** > pick one or more **Groups** > Save. They can then open only the dashboard areas and admin tables
their role allows (others return 403). A role-only account cannot create users or edit groups, so the first account
must be the superuser.

## Notes
* Scanning book covers uses the API engines (Gemini / OCR.space / Google Vision) first; Tesseract is
  only a last resort and is not installed, so add at least one API key if you want that feature.
* The ML models in `ml_models/` are optional and are ignored on Wasmer (the ML libraries are not installed there).
  To use them, install `requirements-optional.txt` on a host with more build memory and run
  `python manage.py train_recommendations` / `train_price_model`.
* Python on Wasmer Edge was in beta when this was prepared; if a package fails to install, check the build log
  and the package index at https://python-registry.wasmer.app/all/simple/.
