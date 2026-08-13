"""IMAP poll helper for explicit reconcile (not GET).

ADR 0019 Phase A: conversation/inbox GET must not call this.
Phase B may invoke it from Celery or POST reconcile.
"""

from __future__ import annotations

import logging

from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def poll_guest_inbox_on_force_sync(tenant: Tenant, *, sync_param: str) -> None:
    """Poll tenant IMAP inbox when client requests full sync (sync=1)."""
    if sync_param != "1":
        return
    try:
        poll_tenant_guest_inbox(tenant)
    except Exception:
        logger.exception(
            "guest imap poll on force sync failed",
            extra={"tenant_slug": tenant.slug},
        )
