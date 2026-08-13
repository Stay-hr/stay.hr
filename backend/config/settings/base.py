from pathlib import Path
from zoneinfo import ZoneInfo

import environ
from celery.schedules import crontab

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    DJANGO_CSRF_TRUSTED_ORIGINS=(list, []),
    DB_CONN_MAX_AGE=(int, 60),
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

environ.Env.read_env(BASE_DIR.parent / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
STAY_INTEGRATION_FERNET_KEY = env("STAY_INTEGRATION_FERNET_KEY", default="")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = env("WHATSAPP_WEBHOOK_VERIFY_TOKEN", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", default="v23.0")
ALLOW_WHATSAPP_CONNECT_TOKEN = env.bool("ALLOW_WHATSAPP_CONNECT_TOKEN", default=False)
META_APP_ID = env("META_APP_ID", default="")
CF_DNS_API_TOKEN = env("CF_DNS_API_TOKEN", default="")
STAY_SERVER_IP = env("STAY_SERVER_IP", default="")
CLOUDFLARE_ZONE_STAY = env("CLOUDFLARE_ZONE_STAY", default="stay.hr")
STAY_API_INTERNAL_URL = env("STAY_API_INTERNAL_URL", default="http://stay_django:8000")
STAY_PUBLIC_API_URL = env("STAY_PUBLIC_API_URL", default="https://api.stay.hr")
FIREBASE_SERVICE_ACCOUNT_PATH = env("FIREBASE_SERVICE_ACCOUNT_PATH", default="")
FIREBASE_PROJECT_ID = env("FIREBASE_PROJECT_ID", default="hospira-fc0dc")
FCM_PUSH_ENABLED = env.bool("FCM_PUSH_ENABLED", default=True)
FCM_PUSH_MAINTENANCE = env.bool("FCM_PUSH_MAINTENANCE", default=False)
WHATSAPP_AUTOCHECKIN_MAINTENANCE = env.bool("WHATSAPP_AUTOCHECKIN_MAINTENANCE", default=False)
WHATSAPP_DOCUMENT_INTAKE_LIFECYCLE_GATE = env.bool(
    "WHATSAPP_DOCUMENT_INTAKE_LIFECYCLE_GATE",
    default=True,
)
GUEST_CHECKIN_WEB_ONLY = env.bool("GUEST_CHECKIN_WEB_ONLY", default=True)
# OCR residence address: FR postal street-first → City, street (fail-closed via eVisitor validator)
OCR_ADDRESS_NORMALIZATION_ENABLED = env.bool(
    "OCR_ADDRESS_NORMALIZATION_ENABLED",
    default=True,
)
FCM_PUSH_ALLOWED_TENANT_SLUGS = env.list("FCM_PUSH_ALLOWED_TENANT_SLUGS", default=[])
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.core",
    "apps.tenants",
    "apps.properties",
    "apps.reservations",
    "apps.integrations",
    "apps.communications",
    "apps.legacy_import",
    "apps.tourist_tax",
    "apps.billing",
    "apps.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.tenants.middleware.TenantHostMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if DEBUG:
    MIDDLEWARE.insert(0, "apps.core.dev_cors.DevCorsMiddleware")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="stay_platform_db"),
        "USER": env("DB_USER", default="stay"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", default="postgis"),
        "PORT": env("DB_PORT", default="5432"),
        "CONN_MAX_AGE": env("DB_CONN_MAX_AGE"),
    }
}

_uzorita_db_name = env("UZORITA_DB_NAME", default="")
if _uzorita_db_name:
    DATABASES["uzorita_legacy"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _uzorita_db_name,
        "USER": env("UZORITA_DB_USER", default="postgres"),
        "PASSWORD": env("UZORITA_DB_PASSWORD", default=""),
        "HOST": env("UZORITA_DB_HOST", default="localhost"),
        "PORT": env("UZORITA_DB_PORT", default="5432"),
        "CONN_MAX_AGE": env("DB_CONN_MAX_AGE"),
        "OPTIONS": {
            "options": "-c default_transaction_read_only=on",
        },
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DOCUMENT_PHOTO_MAX_BYTES = 8 * 1024 * 1024

# Unit listing photos (ADR 0015) — validation gate before UnitPhoto insert
UNIT_PHOTO_MAX_BYTES = env.int("UNIT_PHOTO_MAX_BYTES", default=8 * 1024 * 1024)
UNIT_PHOTO_MAX_EDGE = env.int("UNIT_PHOTO_MAX_EDGE", default=8000)
UNIT_PHOTO_MIN_EDGE = env.int("UNIT_PHOTO_MIN_EDGE", default=100)

# Channex write capability (single-writer / concurrency control). Fail-closed:
# default False so WSL/stale clones cannot mutate live ARI. hel1 .env MUST set true
# before deploy (see ADR 0014 + incident 2026-08-01). WSL: false via .env / docker-compose.dev.yml.
CHANNEX_OUTBOUND_ENABLED = env.bool("CHANNEX_OUTBOUND_ENABLED", default=False)
# Empty = all tenants when enabled; non-empty = only listed slugs may write.
CHANNEX_OUTBOUND_TENANT_SLUGS = env.list("CHANNEX_OUTBOUND_TENANT_SLUGS", default=[])
# When true, all outbound writes are blocked (ops maintenance).
CHANNEX_OUTBOUND_MAINTENANCE = env.bool("CHANNEX_OUTBOUND_MAINTENANCE", default=False)
# ARI repair blast-radius thresholds (stale-DB proxy). Verify-only ignores these.
CHANNEX_ARI_REPAIR_MAX_UNITS = env.int("CHANNEX_ARI_REPAIR_MAX_UNITS", default=5)
CHANNEX_ARI_REPAIR_MAX_UNIT_PERCENT = env.float(
    "CHANNEX_ARI_REPAIR_MAX_UNIT_PERCENT", default=20.0
)
CHANNEX_ARI_REPAIR_MAX_DAYS_PER_UNIT = env.int(
    "CHANNEX_ARI_REPAIR_MAX_DAYS_PER_UNIT", default=3
)

# After create_photo, GET /photos/:id to confirm id/position/room_type (Phase B rollout).
UNIT_PHOTO_VERIFY_AFTER_UPLOAD = env.bool("UNIT_PHOTO_VERIFY_AFTER_UPLOAD", default=True)

# ChannelSyncObserver on GET /system/status photos (ADR 0015 Layer 2/3). Default off
# (CLI is primary); set true for live Channex LIST on status polls.
CHANNEL_PHOTO_STATUS_ENABLED = env.bool("CHANNEL_PHOTO_STATUS_ENABLED", default=False)

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@stay.hr")

DOCUMENT_INTAKE_QUALITY_REPORT_ENABLED = env.bool(
    "DOCUMENT_INTAKE_QUALITY_REPORT_ENABLED", default=False
)
DOCUMENT_INTAKE_QUALITY_REPORT_EMAIL = env(
    "DOCUMENT_INTAKE_QUALITY_REPORT_EMAIL", default=""
)
DOCUMENT_INTAKE_QUALITY_REPORT_DAYS = env.int(
    "DOCUMENT_INTAKE_QUALITY_REPORT_DAYS", default=7
)
DOCUMENT_INTAKE_QUALITY_REPORT_SEND_EVERY_DAYS = env.int(
    "DOCUMENT_INTAKE_QUALITY_REPORT_SEND_EVERY_DAYS", default=1
)
DOCUMENT_INTAKE_QUALITY_REPORT_MONDAY_ONLY = env.bool(
    "DOCUMENT_INTAKE_QUALITY_REPORT_MONDAY_ONLY", default=False
)
DOCUMENT_INTAKE_REPORT_SNAPSHOT_PATH = env(
    "DOCUMENT_INTAKE_REPORT_SNAPSHOT_PATH", default=""
)

DAILY_OPS_REPORT_ENABLED = env.bool("DAILY_OPS_REPORT_ENABLED", default=False)
DAILY_OPS_REPORT_EMAILS = env("DAILY_OPS_REPORT_EMAILS", default="")
DAILY_OPS_REPORT_TENANT_ID = env.int("DAILY_OPS_REPORT_TENANT_ID", default=2)
DAILY_OPS_REPORT_DB_SAMPLES = env.int("DAILY_OPS_REPORT_DB_SAMPLES", default=50)
DAILY_OPS_REPORT_DISK_WARN_PCT = env.int("DAILY_OPS_REPORT_DISK_WARN_PCT", default=85)
DAILY_OPS_REPORT_DISK_CRIT_PCT = env.int("DAILY_OPS_REPORT_DISK_CRIT_PCT", default=95)
DAILY_OPS_REPORT_KEEP_DAYS = env.int("DAILY_OPS_REPORT_KEEP_DAYS", default=90)
DAILY_OPS_REPORT_CELERY_EXPECTED = env(
    "DAILY_OPS_REPORT_CELERY_EXPECTED", default="celery-worker,celery-beat"
)

# Property financial report — max check_out period length (days, half-open interval)
PROPERTY_FINANCIAL_REPORT_MAX_DAYS = env.int(
    "PROPERTY_FINANCIAL_REPORT_MAX_DAYS", default=90
)
PROPERTY_FINANCIAL_REPORT_EMAIL_ENABLED = env.bool(
    "PROPERTY_FINANCIAL_REPORT_EMAIL_ENABLED", default=False
)

GUEST_CHECKIN_REMINDER_ENABLED = env.bool("GUEST_CHECKIN_REMINDER_ENABLED", default=True)
GUEST_CHECKIN_REMINDER_DAYS_BEFORE = env("GUEST_CHECKIN_REMINDER_DAYS_BEFORE", default="7,0")
GUEST_CHECKIN_METRICS_DAYS = env.int("GUEST_CHECKIN_METRICS_DAYS", default=30)
INVOICE_EMAIL_WAITING_TIMEOUT_DAYS = env.int("INVOICE_EMAIL_WAITING_TIMEOUT_DAYS", default=14)

# Messaging Orchestration Engine (ADR 0010) — rollout flags.
# Fail-closed: ENABLED with empty TENANTS and PROPERTIES → no materialize/send.
MESSAGE_ORCHESTRATION_ENABLED = env.bool("MESSAGE_ORCHESTRATION_ENABLED", default=False)
MESSAGE_ORCHESTRATION_SHADOW = env.bool("MESSAGE_ORCHESTRATION_SHADOW", default=True)
MESSAGE_ORCHESTRATION_TENANTS = env.list("MESSAGE_ORCHESTRATION_TENANTS", default=[])
MESSAGE_ORCHESTRATION_PROPERTIES = env.list("MESSAGE_ORCHESTRATION_PROPERTIES", default=[])
# Ops alerts when all channel_policy providers fail (comma-separated).
OPERATIONS_ALERT_EMAILS = env.list("OPERATIONS_ALERT_EMAILS", default=[])
MESSAGING_ALERT_THROTTLE_SECONDS = env.float("MESSAGING_ALERT_THROTTLE_SECONDS", default=300.0)

# Property Settings shell (ADR 0008). False → all capabilities/tabs off + reception nav hidden.
RECEPTION_PROPERTY_SETTINGS = env.bool("RECEPTION_PROPERTY_SETTINGS", default=True)

# Fiskal Platform v2 Execution API (guest invoice F1 via pu_cis)
FISKAL_PLATFORM_URL = env("FISKAL_PLATFORM_URL", default="http://fiskal-api:8000")
FISKAL_PLATFORM_API_KEY = env("FISKAL_PLATFORM_API_KEY", default="")
FISKAL_EXECUTION_ENABLED = env.bool("FISKAL_EXECUTION_ENABLED", default=False)
FISKAL_EXECUTION_POLL_INTERVAL = env.float("FISKAL_EXECUTION_POLL_INTERVAL", default=2.0)
FISKAL_EXECUTION_POLL_TIMEOUT = env.float("FISKAL_EXECUTION_POLL_TIMEOUT", default=120.0)

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.AppKeyAuthentication",
    ],
    "UNAUTHENTICATED_USER": None,
}

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")

# ADR 0005 Phase 2a — ReservationVersionEventBus (in_process | redis).
# Rollback: set in_process and recreate django + celery-worker.
RESERVATION_VERSION_EVENT_BUS = env(
    "RESERVATION_VERSION_EVENT_BUS", default="in_process"
)

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE


def _zagreb_now():
    from datetime import datetime

    return datetime.now(ZoneInfo("Europe/Zagreb"))


CELERY_BEAT_SCHEDULE = {
    "core-ping": {
        "task": "apps.core.tasks.ping",
        "schedule": 3600.0,
    },
    "auto-checkout": {
        "task": "apps.reservations.tasks.run_auto_checkouts",
        "schedule": 900.0,
    },
    "whatsapp-autocheckin-welcome": {
        "task": "apps.communications.whatsapp_autocheckin_tasks.run_whatsapp_autocheckin_welcome",
        "schedule": 900.0,
    },
    "channex-messages-upcoming-checkins": {
        # ADR 0019 Phase B: A∪B∪C∪D membership; Celery path kept for beat stability.
        "task": "apps.integrations.channex.message_tasks.sync_channex_messages_for_upcoming_checkins",
        "schedule": 900.0,
        "kwargs": {"tenant_slug": "uzorita"},
    },
    "channex-booking-revisions-feed": {
        "task": "apps.integrations.channex.booking_tasks.process_channex_booking_revisions_feed_periodic",
        "schedule": 900.0,
        "kwargs": {"tenant_slug": "uzorita"},
    },
    "channex-reviews-periodic": {
        "task": "apps.integrations.channex.review_tasks.sync_channex_reviews_periodic",
        "schedule": 21600.0,
        "kwargs": {"tenant_slug": "uzorita"},
    },
    "guest-email-imap-poll": {
        "task": "apps.communications.email_ingest_tasks.poll_guest_email_inbox",
        "schedule": 120.0,
        "kwargs": {"tenant_slug": "uzorita"},
    },
    "detect-overbooking-daily": {
        "task": "apps.reservations.overbooking_tasks.detect_overbooking_daily",
        "schedule": crontab(hour=6, minute=0),
        "kwargs": {"tenant_id": 2},
    },
    "detect-multi-room-gaps-daily": {
        "task": "apps.reservations.overbooking_tasks.detect_multi_room_gaps_daily",
        "schedule": crontab(hour=6, minute=15),
        "kwargs": {"tenant_id": 2},
    },
    "verify-channex-availability-daily": {
        "task": "apps.integrations.channex.tasks.verify_channex_availability_daily",
        "schedule": crontab(hour=6, minute=30),
        "kwargs": {"tenant_id": 2},
    },
    "reconcile-channex-cancelled-bookings-daily": {
        "task": "apps.integrations.channex.booking_tasks.reconcile_channex_cancelled_bookings_daily",
        "schedule": crontab(hour=6, minute=45),
        "kwargs": {"tenant_slug": "uzorita"},
    },
    "reconcile-guest-document-batches": {
        "task": "apps.integrations.whatsapp.guest_document_batch_reconcile.reconcile_guest_document_batches",
        "schedule": 900.0,
        "kwargs": {"apply": True},
    },
    "document-intake-quality-report-email": {
        "task": "reservations.send_document_intake_quality_report",
        "schedule": crontab(hour=9, minute=0, nowfun=_zagreb_now),
    },
    "guest-checkin-expire-sessions": {
        "task": "reservations.expire_guest_checkin_sessions",
        "schedule": 3600.0,
    },
    "guest-checkin-pre-arrival-reminders": {
        "task": "communications.send_pre_arrival_checkin_reminders",
        "schedule": 900.0,
    },
    # ADR 0010 — gated by MESSAGE_ORCHESTRATION_* (no-op when disabled).
    "message-orchestration": {
        "task": "communications.run_message_orchestration",
        "schedule": 900.0,
    },
    "guest-checkin-metrics-daily": {
        "task": "reservations.log_guest_checkin_metrics",
        "schedule": crontab(hour=7, minute=30, nowfun=_zagreb_now),
    },
    "daily-ops-report-email": {
        "task": "core.send_daily_ops_report",
        "schedule": crontab(hour=10, minute=0, nowfun=_zagreb_now),
    },
    "property-financial-report-monthly-email": {
        "task": "reservations.send_property_financial_reports_monthly",
        "schedule": crontab(day_of_month=1, hour=8, minute=0, nowfun=_zagreb_now),
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "mask_sensitive_headers": {
            "()": "config.settings.logging.SensitiveHeaderFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["mask_sensitive_headers"],
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
