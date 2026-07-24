"""Property → Tenant → Platform schedule resolve (ADR 0010 §5).

Schedule keys are nullable on Property / TenantReceptionSettings (null = inherit).
Platform defaults live here (not Django settings) so resolve stays linear and
testable without env churn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apps.communications.messaging.models import MessageScheduleStrategy
from apps.core.timezone import DEFAULT_TIMEZONE, effective_timezone
from apps.properties.models import Property
from apps.tenants.models import Tenant, TenantReceptionSettings

# Schedule prefixes used by ReminderPlan.schedule_prefix and resolve chains.
PREFIX_PRE_ARRIVAL = "pre_arrival"
# Legacy column prefix; unread by WELCOME resolve (Korak 2 may drop columns).
PREFIX_WELCOME = "welcome"
PREFIX_WHATSAPP_WELCOME = "whatsapp_welcome"

# WELCOME plan: whatsapp_welcome_* → platform (ADR §5). welcome_* ignored.
WELCOME_RESOLVE_PREFIXES: tuple[str, ...] = (PREFIX_WHATSAPP_WELCOME,)

PRE_ARRIVAL_RESOLVE_PREFIXES: tuple[str, ...] = (PREFIX_PRE_ARRIVAL,)

_VALID_STRATEGIES = frozenset(c.value for c in MessageScheduleStrategy)


@dataclass(frozen=True)
class PlatformScheduleDefault:
    days_before: int
    send_time: time
    schedule_strategy: str


PLATFORM_SCHEDULE_DEFAULTS: dict[str, PlatformScheduleDefault] = {
    PREFIX_PRE_ARRIVAL: PlatformScheduleDefault(
        days_before=7,
        send_time=time(9, 0),
        schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
    ),
    PREFIX_WELCOME: PlatformScheduleDefault(
        days_before=0,
        send_time=time(11, 15),
        schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
    ),
    PREFIX_WHATSAPP_WELCOME: PlatformScheduleDefault(
        days_before=0,
        send_time=time(11, 15),
        schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
    ),
}


@dataclass(frozen=True)
class ResolvedSchedule:
    """Effective schedule after Property → Tenant → Platform resolve."""

    days_before: int
    send_time: time
    schedule_strategy: str
    # Where each field came from (e.g. "property:pre_arrival", "tenant:welcome", "platform").
    days_before_source: str
    send_time_source: str
    schedule_strategy_source: str
    resolve_prefixes: tuple[str, ...]

    @property
    def source_summary(self) -> str:
        return (
            f"days={self.days_before_source} "
            f"time={self.send_time_source} "
            f"strategy={self.schedule_strategy_source}"
        )


@dataclass(frozen=True)
class ComputedDue:
    """Timezone snapshot for MessageDispatch create (ADR §4.B)."""

    due_at: datetime  # UTC-aware
    local_due_at: datetime  # aware in property TZ (same instant as due_at)
    timezone: str  # IANA frozen at create
    schedule_strategy: str
    target_local_date: date


def _blank_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _normalize_strategy(value: Any) -> str | None:
    raw = _blank_to_none(value)
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text not in _VALID_STRATEGIES:
        raise ValueError(
            f"Invalid schedule_strategy {raw!r}; "
            f"expected one of {sorted(_VALID_STRATEGIES)}"
        )
    return text


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    return _blank_to_none(getattr(obj, name, None))


def _tenant_reception_settings(
    tenant: Tenant | None,
    *,
    reception_settings: TenantReceptionSettings | None = None,
) -> TenantReceptionSettings | None:
    if reception_settings is not None:
        return reception_settings
    if tenant is None:
        return None
    return getattr(tenant, "reception_settings", None)


def _platform_default(prefix: str, field: str) -> Any:
    defaults = PLATFORM_SCHEDULE_DEFAULTS.get(prefix)
    if defaults is None:
        # Fall back to pre_arrival platform defaults for unknown prefixes.
        defaults = PLATFORM_SCHEDULE_DEFAULTS[PREFIX_PRE_ARRIVAL]
    if field == "days_before":
        return int(defaults.days_before)
    if field == "send_time":
        return defaults.send_time
    if field == "schedule_strategy":
        return str(defaults.schedule_strategy)
    raise KeyError(field)


def _legacy_whatsapp_send_time(prop: Property | None) -> time | None:
    """Bridge: Property.whatsapp_autocheckin_time → whatsapp_welcome_send_time."""
    if prop is None:
        return None
    return _attr(prop, "whatsapp_autocheckin_time")


def resolve_field(
    *,
    field: str,
    prefixes: tuple[str, ...],
    property: Property | None = None,
    tenant: Tenant | None = None,
    reception_settings: TenantReceptionSettings | None = None,
) -> tuple[Any, str]:
    """Resolve one schedule field. Returns (value, source_label)."""
    settings_row = _tenant_reception_settings(
        tenant, reception_settings=reception_settings
    )

    for prefix in prefixes:
        attr = f"{prefix}_{field}"
        prop_val = _attr(property, attr)
        if prop_val is not None:
            if field == "schedule_strategy":
                prop_val = _normalize_strategy(prop_val)
            return prop_val, f"property:{prefix}"

        # Legacy bridge: only when welcome_send_time unset and autocheck-in enabled.
        if (
            field == "send_time"
            and prefix == PREFIX_WHATSAPP_WELCOME
            and property is not None
            and _attr(property, "whatsapp_welcome_send_time") is None
            and bool(getattr(property, "whatsapp_autocheckin_enabled", False))
        ):
            legacy = _legacy_whatsapp_send_time(property)
            if legacy is not None:
                return legacy, "property:whatsapp_autocheckin_time"

        tenant_val = _attr(settings_row, attr)
        if tenant_val is not None:
            if field == "schedule_strategy":
                tenant_val = _normalize_strategy(tenant_val)
            return tenant_val, f"tenant:{prefix}"

    # Platform: first prefix that has PLATFORM_SCHEDULE_DEFAULTS wins.
    for prefix in prefixes:
        if prefix in PLATFORM_SCHEDULE_DEFAULTS:
            return _platform_default(prefix, field), f"platform:{prefix}"
    return _platform_default(PREFIX_PRE_ARRIVAL, field), "platform:pre_arrival"


def resolve_schedule(
    *,
    prefixes: tuple[str, ...],
    property: Property | None = None,
    tenant: Tenant | None = None,
    reception_settings: TenantReceptionSettings | None = None,
) -> ResolvedSchedule:
    """Resolve days_before / send_time / schedule_strategy for a prefix chain."""
    if not prefixes:
        raise ValueError("resolve_schedule requires at least one prefix")

    days_before, days_src = resolve_field(
        field="days_before",
        prefixes=prefixes,
        property=property,
        tenant=tenant,
        reception_settings=reception_settings,
    )
    send_time, time_src = resolve_field(
        field="send_time",
        prefixes=prefixes,
        property=property,
        tenant=tenant,
        reception_settings=reception_settings,
    )
    strategy, strategy_src = resolve_field(
        field="schedule_strategy",
        prefixes=prefixes,
        property=property,
        tenant=tenant,
        reception_settings=reception_settings,
    )
    strategy = _normalize_strategy(strategy) or MessageScheduleStrategy.FIXED_TIME

    return ResolvedSchedule(
        days_before=max(0, int(days_before)),
        send_time=send_time if isinstance(send_time, time) else time(9, 0),
        schedule_strategy=strategy,
        days_before_source=days_src,
        send_time_source=time_src,
        schedule_strategy_source=strategy_src,
        resolve_prefixes=prefixes,
    )


def resolve_prefixes_for_plan(schedule_prefix: str) -> tuple[str, ...]:
    """Map ReminderPlan.schedule_prefix → resolve chain."""
    if schedule_prefix == PREFIX_WHATSAPP_WELCOME:
        return WELCOME_RESOLVE_PREFIXES
    if schedule_prefix == PREFIX_WELCOME:
        return (PREFIX_WELCOME,)
    if schedule_prefix == PREFIX_PRE_ARRIVAL:
        return PRE_ARRIVAL_RESOLVE_PREFIXES
    return (schedule_prefix,)


def resolve_schedule_for_plan(
    schedule_prefix: str,
    *,
    property: Property | None = None,
    tenant: Tenant | None = None,
    reception_settings: TenantReceptionSettings | None = None,
) -> ResolvedSchedule:
    return resolve_schedule(
        prefixes=resolve_prefixes_for_plan(schedule_prefix),
        property=property,
        tenant=tenant,
        reception_settings=reception_settings,
    )


def property_timezone_name(prop: Property | None, tenant: Tenant | None = None) -> str:
    try:
        name = effective_timezone(property=prop, tenant=tenant)
        ZoneInfo(name)
        return name
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE


def compute_due(
    *,
    check_in: date,
    schedule: ResolvedSchedule,
    timezone_name: str,
    now: datetime,
    strategy_override: str | None = None,
) -> ComputedDue:
    """Compute due_at / local_due_at snapshot for a TIME trigger.

    Strategies (ADR §4.A):
    - FIXED_TIME: local (check_in − days_before) @ send_time
    - FIRST_AFTER: floor = FIXED_TIME; if ``now`` is already on/after floor, due_at = now
    - IMMEDIATE: due_at = now (at materialization)
    """
    strategy = strategy_override or schedule.schedule_strategy
    strategy = _normalize_strategy(strategy) or MessageScheduleStrategy.FIXED_TIME

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        timezone_name = DEFAULT_TIMEZONE

    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    now_utc = now.astimezone(ZoneInfo("UTC"))
    now_local = now.astimezone(tz)

    target_local_date = check_in - timedelta(days=int(schedule.days_before))
    floor_local = datetime.combine(target_local_date, schedule.send_time, tzinfo=tz)
    floor_utc = floor_local.astimezone(ZoneInfo("UTC"))

    if strategy == MessageScheduleStrategy.IMMEDIATE:
        due_local = now_local
    elif strategy == MessageScheduleStrategy.FIRST_AFTER:
        # First scheduler opportunity on/after local send_time that day.
        if now_utc >= floor_utc:
            due_local = now_local
        else:
            due_local = floor_local
    else:
        # FIXED_TIME
        due_local = floor_local

    due_utc = due_local.astimezone(ZoneInfo("UTC"))
    return ComputedDue(
        due_at=due_utc,
        local_due_at=due_local,
        timezone=timezone_name,
        schedule_strategy=strategy,
        target_local_date=target_local_date,
    )


def schedule_to_effective_dict(schedule: ResolvedSchedule) -> dict[str, Any]:
    """API/admin-friendly effective schedule + inheritance hints."""
    return {
        "days_before": schedule.days_before,
        "send_time": schedule.send_time.strftime("%H:%M"),
        "schedule_strategy": schedule.schedule_strategy,
        "inherited": {
            "days_before": schedule.days_before_source,
            "send_time": schedule.send_time_source,
            "schedule_strategy": schedule.schedule_strategy_source,
        },
        "resolve_prefixes": list(schedule.resolve_prefixes),
    }


def platform_defaults_dict() -> Mapping[str, Mapping[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for prefix, defaults in PLATFORM_SCHEDULE_DEFAULTS.items():
        out[prefix] = {
            "days_before": defaults.days_before,
            "send_time": defaults.send_time.strftime("%H:%M"),
            "schedule_strategy": defaults.schedule_strategy,
        }
    return out
