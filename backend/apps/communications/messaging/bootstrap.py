"""Bootstrap v1 messaging catalogs + startup validation (ADR 0010)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

from apps.communications.messaging.definitions import (
    ChannelPolicy,
    DedupePolicy,
    MessageDefinition,
    definition_registry,
)
from apps.communications.messaging.intents import MessageDefinitionKey
from apps.communications.messaging.models import (
    MessageRecipientType,
    MessageScheduleStrategy,
)
from apps.communications.messaging.plans import build_v1_plans, plan_registry
from apps.communications.messaging.providers.factory import build_live_providers
from apps.communications.messaging.providers.registry import provider_registry
from apps.communications.messaging.skip_rules import register_builtin_skip_rules
from apps.communications.messaging.templates import template_registry
from apps.communications.messaging.validation import validate_messaging_engine

logger = logging.getLogger(__name__)

_bootstrapped = False
_lock = threading.Lock()

# Template version keys (unique across definitions).
TEMPLATE_CHECKIN_INFO = "checkin_info@v1"
TEMPLATE_CHECKIN_LINK = "checkin_link@v1"
TEMPLATE_WELCOME = "welcome@v1"


def _placeholder_renderer(body: str, subject: str = ""):
    def _render(dispatch, ctx, base_context: Mapping[str, Any]):
        merged = dict(base_context)
        # CHECKIN_LINK may carry session URL in render_context (Phase 5/7).
        url = str(merged.get("checkin_url") or merged.get("session_url") or "")
        text = body
        if url and "{checkin_url}" in text:
            text = text.replace("{checkin_url}", url)
        return text, subject, merged

    return _render


def _register_providers(*, force: bool) -> None:
    if force:
        provider_registry.clear()
    if provider_registry.names():
        return
    for provider in build_live_providers():
        provider_registry.register(provider)


def _register_templates(*, force: bool) -> None:
    if force:
        template_registry.clear()
    if template_registry.renderer_keys():
        return
    template_registry.register(
        renderer_key=str(MessageDefinitionKey.CHECKIN_INFO),
        template_version=TEMPLATE_CHECKIN_INFO,
        renderer=_placeholder_renderer(
            "Your check-in details will be available soon.",
            subject="Check-in information",
        ),
    )
    template_registry.register(
        renderer_key=str(MessageDefinitionKey.CHECKIN_LINK),
        template_version=TEMPLATE_CHECKIN_LINK,
        renderer=_placeholder_renderer(
            "Complete your online check-in: {checkin_url}",
            subject="Online check-in",
        ),
    )
    template_registry.register(
        renderer_key=str(MessageDefinitionKey.WELCOME),
        template_version=TEMPLATE_WELCOME,
        renderer=_placeholder_renderer(
            # Meta WhatsApp welcome template copy stays on the provider (Phase 4/7);
            # body here is a render snapshot placeholder for checksum / audit.
            "Welcome — WhatsApp template send.",
            subject="",
        ),
    )


def _register_definitions(*, force: bool) -> None:
    if force:
        definition_registry.clear()
    if definition_registry.keys():
        return

    definition_registry.register(
        MessageDefinition(
            key=MessageDefinitionKey.CHECKIN_INFO,
            template_version=TEMPLATE_CHECKIN_INFO,
            channel_policy=ChannelPolicy(
                providers=("booking", "email", "whatsapp")
            ),
            skip_rule_names=("expired", "archived"),
            audience=MessageRecipientType.BOOKER,
            dedupe=DedupePolicy(enabled=True),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            renderer_key=MessageDefinitionKey.CHECKIN_INFO,
        )
    )
    definition_registry.register(
        MessageDefinition(
            key=MessageDefinitionKey.CHECKIN_LINK,
            template_version=TEMPLATE_CHECKIN_LINK,
            channel_policy=ChannelPolicy(
                providers=("booking", "email", "whatsapp")
            ),
            skip_rule_names=("expired", "archived"),
            audience=MessageRecipientType.BOOKER,
            dedupe=DedupePolicy(enabled=True),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            renderer_key=MessageDefinitionKey.CHECKIN_LINK,
        )
    )
    definition_registry.register(
        MessageDefinition(
            key=MessageDefinitionKey.WELCOME,
            template_version=TEMPLATE_WELCOME,
            channel_policy=ChannelPolicy(providers=("whatsapp",)),
            skip_rule_names=("expired", "archived"),
            audience=MessageRecipientType.BOOKER,
            dedupe=DedupePolicy(enabled=True),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            renderer_key=MessageDefinitionKey.WELCOME,
        )
    )


def _register_plans(*, force: bool) -> None:
    if force:
        plan_registry.clear()
    if plan_registry.keys():
        return
    for plan in build_v1_plans():
        plan_registry.register(plan)


def bootstrap_messaging_engine(*, force: bool = False, validate: bool = True) -> None:
    """Idempotent process bootstrap for registries + fail-fast validation."""
    global _bootstrapped
    with _lock:
        if _bootstrapped and not force:
            return
        register_builtin_skip_rules()
        _register_providers(force=force)
        _register_templates(force=force)
        _register_definitions(force=force)
        _register_plans(force=force)
        if validate:
            validate_messaging_engine(raise_on_error=True)
        _bootstrapped = True
        logger.info(
            "messaging_engine_bootstrapped definitions=%s providers=%s plans=%s templates=%s",
            list(definition_registry.keys()),
            list(provider_registry.names()),
            list(plan_registry.keys()),
            list(template_registry.renderer_keys()),
        )


def reset_messaging_engine_for_tests() -> None:
    """Clear catalogs and bootstrap flag (tests only)."""
    global _bootstrapped
    with _lock:
        definition_registry.clear()
        template_registry.clear()
        provider_registry.clear()
        plan_registry.clear()
        from apps.communications.messaging.alerts import reset_alert_throttle_for_tests
        from apps.communications.messaging.skip_rules import skip_rule_engine
        from apps.communications.messaging.middleware import middleware_registry

        skip_rule_engine.clear()
        middleware_registry.clear()
        reset_alert_throttle_for_tests()
        _bootstrapped = False
