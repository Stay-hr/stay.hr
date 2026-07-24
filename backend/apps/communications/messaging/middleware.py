"""Dispatcher middleware hooks — interface only in v1 (ADR 0010 §4.G).

Middleware must never crash the engine: each hook is isolated.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch

logger = logging.getLogger(__name__)


class DispatcherMiddleware(Protocol):
    def before_dispatch(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> None: ...

    def after_dispatch(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
        outcome: Any,
    ) -> None: ...


class NoOpMiddleware:
    """Default middleware — reserved for analytics / GDPR / A/B / billing."""

    def before_dispatch(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> None:
        return None

    def after_dispatch(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
        outcome: Any,
    ) -> None:
        return None


class MiddlewareRegistry:
    def __init__(self) -> None:
        self._hooks: list[DispatcherMiddleware] = []

    def clear(self) -> None:
        self._hooks.clear()

    def register(self, middleware: DispatcherMiddleware) -> None:
        self._hooks.append(middleware)

    def all(self) -> tuple[DispatcherMiddleware, ...]:
        return tuple(self._hooks)

    def before_dispatch(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> None:
        for hook in self._hooks:
            try:
                hook.before_dispatch(dispatch, ctx)
            except Exception:  # noqa: BLE001 — middleware isolation
                logger.exception(
                    "messaging_middleware_before_failed dispatch_id=%s hook=%s",
                    getattr(dispatch, "pk", None),
                    type(hook).__name__,
                )

    def after_dispatch(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
        outcome: Any,
    ) -> None:
        for hook in self._hooks:
            try:
                hook.after_dispatch(dispatch, ctx, outcome)
            except Exception:  # noqa: BLE001 — middleware isolation
                logger.exception(
                    "messaging_middleware_after_failed dispatch_id=%s hook=%s",
                    getattr(dispatch, "pk", None),
                    type(hook).__name__,
                )


middleware_registry = MiddlewareRegistry()
