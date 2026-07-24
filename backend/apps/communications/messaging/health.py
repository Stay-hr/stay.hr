"""Messaging health inventory (ADR 0010 §10) — queue depth for status later."""

from __future__ import annotations

from typing import Any

from django.db.models import Max
from django.utils import timezone

from apps.communications.messaging.definitions import definition_registry
from apps.communications.messaging.flags import flags_health_snapshot
from apps.communications.messaging.models import (
    MessageDeliveryAttempt,
    MessageDispatch,
    MessageDispatchStatus,
)
from apps.communications.messaging.plans import plan_registry
from apps.communications.messaging.providers.registry import provider_registry
from apps.communications.messaging.templates import template_registry


def messaging_health_snapshot(*, include_queue: bool = True) -> dict[str, Any]:
    """In-memory catalog inventory + optional outbox queue depth."""
    providers = []
    for provider in provider_registry.all():
        providers.append(
            {
                "name": provider.name,
                "channel": provider.channel,
                "capabilities": {
                    "channels": sorted(provider.capabilities.channels),
                    "supports_attachments": provider.capabilities.supports_attachments,
                    "supports_templates": provider.capabilities.supports_templates,
                },
            }
        )

    snapshot: dict[str, Any] = {
        "flags": flags_health_snapshot(),
        "definitions": {
            "count": len(definition_registry),
            "keys": list(definition_registry.keys()),
        },
        "templates": {
            "count": len(template_registry.renderer_keys()),
            "renderer_keys": list(template_registry.renderer_keys()),
            "template_versions": list(template_registry.template_versions()),
        },
        "providers": {
            "count": len(provider_registry),
            "items": providers,
        },
        "plans": {
            "count": len(plan_registry),
            "keys": list(plan_registry.keys()),
        },
        "generated_at": timezone.now().isoformat(),
    }

    if include_queue:
        planned = MessageDispatch.objects.filter(
            status=MessageDispatchStatus.PLANNED,
            archived_at__isnull=True,
        ).count()
        queued = MessageDispatch.objects.filter(
            status=MessageDispatchStatus.QUEUED,
            archived_at__isnull=True,
        ).count()
        last_success = (
            MessageDeliveryAttempt.objects.filter(success=True)
            .aggregate(ts=Max("created_at"))
            .get("ts")
        )
        last_failure = (
            MessageDeliveryAttempt.objects.filter(success=False)
            .aggregate(ts=Max("created_at"))
            .get("ts")
        )
        snapshot["outbox"] = {
            "planned": planned,
            "queued": queued,
            "depth": planned + queued,
        }
        snapshot["last_success_at"] = (
            last_success.isoformat() if last_success else None
        )
        snapshot["last_failure_at"] = (
            last_failure.isoformat() if last_failure else None
        )

    return snapshot
