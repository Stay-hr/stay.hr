"""Postgres test settings — dedicated DB on shared PostGIS container (postgis network)."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")

from config.settings.base import *  # noqa: F403

_TEST_DB_NAME = env("TEST_DB_NAME", default="stay_platform_test_db")

DATABASES["default"]["NAME"] = _TEST_DB_NAME
# Separate test DB so manage.py migrate on NAME does not share state with
# TestCase/TransactionTestCase runs (critical for GitHub Actions keepdb).
DATABASES["default"]["TEST"] = {"NAME": f"test_{_TEST_DB_NAME}"}
DATABASES.pop("uzorita_legacy", None)

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
