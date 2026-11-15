"""Single-flight guard for guest IMAP work (live polling and replay).

One tenant mailbox must be worked by one process at a time: the beat task, the
``poll_guest_email`` CLI and ``replay_guest_email`` all take this lock. A second
caller does not wait: it gets ``acquired=False`` and must skip.

Postgres *session-level* advisory lock (the work spans IMAP network I/O, so a
transaction-scoped lock would pin a transaction open). Properties that matter here:

- non-blocking: no queue of waiting workers behind a slow poll;
- released in ``finally`` and, if the process dies, by Postgres when its connection
  drops, so there is no stale-lock/TTL problem (unlike a Redis lock);
- keyed per tenant: tenants never block each other.

The database is reached directly (no PgBouncer), so session locks are reliable. On a
non-PostgreSQL backend (sqlite test settings) the guard is a no-op that always grants.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection

logger = logging.getLogger(__name__)

# int4 namespace ("GIMP"); the tenant id is the second key of pg_try_advisory_lock(int4, int4).
LOCK_NAMESPACE = 0x47494D50


@contextmanager
def guest_imap_single_flight(tenant_id: int) -> Iterator[bool]:
    """Yield True if this caller now owns the tenant's IMAP lock, False if it is taken."""
    if connection.vendor != "postgresql":
        yield True
        return

    key = (LOCK_NAMESPACE, int(tenant_id))
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", key)
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s, %s)", key)
            except Exception:  # connection already gone => Postgres released the lock
                logger.warning("guest imap lock release failed tenant_id=%s", tenant_id, exc_info=True)
