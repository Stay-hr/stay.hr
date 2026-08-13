"""Process-level runtime stats for observability endpoints.

All values are **per Gunicorn worker process**. Counters reset when a worker
is recycled (``--max-requests``) or the container restarts. They are not shared
across workers and are not global cluster totals.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timezone

# Bumped when adding top-level ``conversation`` ingest stamps (ADR 0019 Phase B).
SYSTEM_STATUS_SCHEMA_VERSION = 4

_STARTED_AT_MONO = time.monotonic()
_STARTED_AT_ISO = datetime.now(timezone.utc).isoformat()


def worker_uptime_seconds() -> int:
    return int(time.monotonic() - _STARTED_AT_MONO)


def gunicorn_config_from_env() -> dict[str, int | str]:
    return {
        "workers": int(os.environ.get("GUNICORN_WORKERS", "8")),
        "worker_class": os.environ.get("GUNICORN_WORKER_CLASS", "sync"),
    }


def build_info_from_env() -> dict[str, str]:
    return {
        "git_sha": os.environ.get("STAY_GIT_SHA", "unknown"),
        "started_at": _STARTED_AT_ISO,
        "hostname": socket.gethostname(),
    }
