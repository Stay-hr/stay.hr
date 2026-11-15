"""Celery tasks for guest email IMAP polling."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings as django_settings

from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@shared_task
def poll_guest_email_inbox(
    tenant_id: int | None = None,
    *,
    tenant_slug: str | None = None,
) -> dict:
    """Poll IMAP inboxes for tenants with guest IMAP enabled.

    Gated by the process-level ``GUEST_IMAP_CELERY_POLL_ENABLED`` flag (default off). The flag is read
    first: when it is off the task returns before touching the database or opening any IMAP session.
    ``TenantReceptionSettings.guest_imap_enabled`` stays the per-tenant control on top of it.
    """
    if not getattr(django_settings, "GUEST_IMAP_CELERY_POLL_ENABLED", False):
        logger.info("guest email imap poll skipped: GUEST_IMAP_CELERY_POLL_ENABLED is off")
        return {"disabled": True}

    tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE).select_related(
        "reception_settings"
    )
    if tenant_id is not None:
        tenants = tenants.filter(pk=tenant_id)
    elif tenant_slug:
        tenants = tenants.filter(slug=tenant_slug)

    summary = {
        "tenants": 0,
        "ingested": 0,
        "skipped": 0,
        "errors": 0,
        "locked": 0,
        "blocked": 0,
        "issues_open": 0,
        "quarantined": 0,
    }

    for tenant in tenants:
        settings = getattr(tenant, "reception_settings", None)
        if settings is None:
            continue
        if not settings.guest_imap_enabled or not settings.has_guest_smtp_password:
            continue

        summary["tenants"] += 1
        result = poll_tenant_guest_inbox(tenant)
        summary["ingested"] += result.ingested
        summary["skipped"] += result.skipped
        summary["errors"] += result.errors
        summary["locked"] += int(result.skipped_locked)
        summary["blocked"] += int(bool(result.blocked_reason))
        summary["issues_open"] += result.issues_open
        summary["quarantined"] += result.quarantined

        logger.info(
            "guest email imap poll finished",
            extra={
                "tenant_slug": tenant.slug,
                "ingested": result.ingested,
                "skipped": result.skipped,
                "errors": result.errors,
                "locked": result.skipped_locked,
                "blocked_reason": result.blocked_reason,
                "issues_open": result.issues_open,
                "quarantined": result.quarantined,
                "max_uid": result.max_uid,
            },
        )

    return summary
