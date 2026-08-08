"""Postgres test settings — dedicated DB on shared PostGIS container (postgis network)."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")
# WhatsApp send paths read os.environ (not Django settings). CI has no host .env token;
# without this, send_credentials_ok() returns missing_credentials on Actions.
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "ci-test-whatsapp-access-token")

from config.settings.base import *  # noqa: F403

_TEST_DB_NAME = env("TEST_DB_NAME", default="stay_platform_test_db")

DATABASES["default"]["NAME"] = _TEST_DB_NAME
# Separate test DB so manage.py migrate on NAME does not share state with
# TestCase/TransactionTestCase runs (critical for GitHub Actions keepdb).
DATABASES["default"]["TEST"] = {"NAME": f"test_{_TEST_DB_NAME}"}
DATABASES.pop("uzorita_legacy", None)

# CI has no host .env DJANGO_ALLOWED_HOSTS; DisallowedHost → 400 on app/admin hosts.
# Leading-dot entries match all subdomains (Django ALLOWED_HOSTS semantics).
ALLOWED_HOSTS = list(
    dict.fromkeys(
        [
            *ALLOWED_HOSTS,
            "testserver",
            "localhost",
            "127.0.0.1",
            "app.stay.hr",
            "admin.stay.hr",
            "api.stay.hr",
            ".stay.hr",
        ]
    )
)

# Manifest storage needs collectstatic; admin template tests only need plain URLs.
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

STAY_INTEGRATION_FERNET_KEY = "M8U_DJpQILQrKpxTOVtRrQp3nR0LJHAl2X0x-7JOH5k="

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
RESERVATION_VERSION_EVENT_BUS = "in_process"

FCM_PUSH_ENABLED = False
FCM_PUSH_ALLOWED_TENANT_SLUGS = []
# Integration / lifecycle suites exercise WA automation paths; production default is web-only.
GUEST_CHECKIN_WEB_ONLY = False
WHATSAPP_AUTOCHECKIN_MAINTENANCE = False
MESSAGE_ORCHESTRATION_ENABLED = False
MESSAGE_ORCHESTRATION_SHADOW = True
MESSAGE_ORCHESTRATION_TENANTS = []
MESSAGE_ORCHESTRATION_PROPERTIES = []

# Photo flush / Channex write tests control these explicitly; default allow writes.
CHANNEX_OUTBOUND_ENABLED = True
CHANNEX_OUTBOUND_TENANT_SLUGS = []
CHANNEX_OUTBOUND_MAINTENANCE = False
UNIT_PHOTO_VERIFY_AFTER_UPLOAD = True
