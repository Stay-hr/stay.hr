"""ReminderPlan catalog — code + settings-backed (not DB DSL) (ADR 0010)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from apps.communications.messaging.intents import (
    PRE_ARRIVAL_INTENTS,
    WELCOME_INTENTS,
)


PLAN_PRE_ARRIVAL = "pre_arrival"
PLAN_WELCOME = "welcome"


@dataclass(frozen=True)
class ReminderPlan:
    """TIME trigger source; offsets/clocks come from resolved schedule settings."""

    key: str
    definition_keys: tuple[str, ...]
    # Prefix for Property → Tenant → Platform schedule keys.
    schedule_prefix: str

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("ReminderPlan.key is required")
        if not self.definition_keys:
            raise ValueError(
                f"ReminderPlan {self.key!r} requires definition_keys"
            )


class PlanRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, ReminderPlan] = {}

    def clear(self) -> None:
        self._by_key.clear()

    def register(self, plan: ReminderPlan) -> None:
        if plan.key in self._by_key:
            raise ValueError(f"Duplicate ReminderPlan key: {plan.key!r}")
        self._by_key[plan.key] = plan

    def get(self, key: str) -> ReminderPlan:
        try:
            return self._by_key[key]
        except KeyError as exc:
            raise KeyError(f"Unknown ReminderPlan: {key!r}") from exc

    def has(self, key: str) -> bool:
        return key in self._by_key

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_key))

    def all(self) -> tuple[ReminderPlan, ...]:
        return tuple(self._by_key[k] for k in sorted(self._by_key))

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self) -> Iterable[ReminderPlan]:
        return iter(self.all())


plan_registry = PlanRegistry()


def build_v1_plans() -> tuple[ReminderPlan, ...]:
    return (
        ReminderPlan(
            key=PLAN_PRE_ARRIVAL,
            definition_keys=tuple(PRE_ARRIVAL_INTENTS),
            schedule_prefix="pre_arrival",
        ),
        ReminderPlan(
            key=PLAN_WELCOME,
            definition_keys=tuple(WELCOME_INTENTS),
            # WELCOME resolves whatsapp_welcome_* → platform.
            schedule_prefix="whatsapp_welcome",
        ),
    )
