"""SQLite settings for local/CI tests without Postgres."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DB_PASSWORD", "test")

from config.settings.base import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Fixed Fernet key for integration config tests (generate: Fernet.generate_key())
STAY_INTEGRATION_FERNET_KEY = "M8U_DJpQILQrKpxTOVtRrQp3nR0LJHAl2X0x-7JOH5k="

# Bez Redis-a u lokalnim/CI testovima (Reservation post_save šalje Celery taskove).
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
RESERVATION_VERSION_EVENT_BUS = "in_process"

FCM_PUSH_ENABLED = False
FCM_PUSH_ALLOWED_TENANT_SLUGS = []
MESSAGE_ORCHESTRATION_ENABLED = False
MESSAGE_ORCHESTRATION_SHADOW = True
MESSAGE_ORCHESTRATION_TENANTS = []
MESSAGE_ORCHESTRATION_PROPERTIES = []
