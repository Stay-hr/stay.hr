"""SkipRule engine — can_send only (ADR 0010)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch


@dataclass(frozen=True)
class SkipDecision:
    can_send: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> SkipDecision:
        return cls(can_send=True)

    @classmethod
    def skip(cls, reason: str) -> SkipDecision:
        return cls(can_send=False, reason=reason or "skipped")


class SkipRule(Protocol):
    name: str

    def evaluate(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> SkipDecision: ...


SkipRuleFn = Callable[[MessageDispatch, TriggerContext], SkipDecision]


@dataclass(frozen=True)
class CallableSkipRule:
    name: str
    fn: SkipRuleFn

    def evaluate(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> SkipDecision:
        return self.fn(dispatch, ctx)


class SkipRuleEngine:
    """Named skip rules; evaluate in definition order until one blocks."""

    def __init__(self) -> None:
        self._rules: dict[str, SkipRule] = {}

    def clear(self) -> None:
        self._rules.clear()

    def register(self, rule: SkipRule) -> None:
        if rule.name in self._rules:
            raise ValueError(f"Duplicate SkipRule name: {rule.name!r}")
        self._rules[rule.name] = rule

    def register_fn(self, name: str, fn: SkipRuleFn) -> None:
        self.register(CallableSkipRule(name=name, fn=fn))

    def get(self, name: str) -> SkipRule:
        try:
            return self._rules[name]
        except KeyError as exc:
            raise KeyError(f"Unknown SkipRule: {name!r}") from exc

    def has(self, name: str) -> bool:
        return name in self._rules

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def can_send(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
        *,
        rule_names: tuple[str, ...],
    ) -> SkipDecision:
        for name in rule_names:
            decision = self.get(name).evaluate(dispatch, ctx)
            if not decision.can_send:
                return decision
        return SkipDecision.allow()


skip_rule_engine = SkipRuleEngine()


def _skip_if_expired(
    dispatch: MessageDispatch,
    ctx: TriggerContext,
) -> SkipDecision:
    if dispatch.expires_at is not None and dispatch.expires_at <= ctx.effective_now:
        return SkipDecision.skip("expired")
    return SkipDecision.allow()


def _skip_if_archived(
    dispatch: MessageDispatch,
    _ctx: TriggerContext,
) -> SkipDecision:
    if dispatch.archived_at is not None:
        return SkipDecision.skip("archived")
    return SkipDecision.allow()


def register_builtin_skip_rules(engine: SkipRuleEngine | None = None) -> None:
    """Idempotent builtins used by v1 definitions / dispatcher."""
    target = engine or skip_rule_engine
    builtins = (
        ("expired", _skip_if_expired),
        ("archived", _skip_if_archived),
    )
    for name, fn in builtins:
        if not target.has(name):
            target.register_fn(name, fn)
