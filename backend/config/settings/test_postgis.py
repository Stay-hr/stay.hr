"""Postgres test settings — dedicated DB on shared PostGIS container (postgis network)."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")

from config.settings.base import *  # noqa: F403

_TEST_DB_NAME = env("TEST_DB_NAME", default="stay_platform_test_db")

DATABASES["default"]["NAME"] = _TEST_DB_NAME
DATABASES["default"]["TEST"] = {"NAME": _TEST_DB_NAME}
DATABASES.pop("uzorita_legacy", None)

STAY_INTEGRATION_FERNET_KEY = "M8U_DJpQILQrKpxTOVtRrQp3nR0LJHAl2X0x-7JOH5k="

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
