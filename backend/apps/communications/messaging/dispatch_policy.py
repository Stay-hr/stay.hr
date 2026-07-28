"""Channel-keyed dispatch policy (ALLOW / DEFER / BLOCK) — ADR 0010 extension.

Business rules live here; the dispatcher only executes ``PolicyDecision``.
Quiet hours key off delivery **channel** (e.g. WhatsApp), never provider brand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.communications.models import GuestMessageChannel
from apps.communications.messaging.models import MessageDispatch


class PolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    DEFER = "defer"
    BLOCK = "block"


@dataclass(frozen=True)
class PolicyDecision:
    kind: PolicyDecisionKind
    reason: str = ""
    next_attempt_at: datetime | None = None  # aware UTC when DEFER
    timezone: str = ""  # IANA from dispatch snapshot
    channel: str = ""


@dataclass(frozen=True)
class ChannelQuietWindow:
    """Local wall-clock window when a channel must not send (half-open [start, end)).

    When ``start > end`` the window wraps midnight (e.g. 21:00–08:00).
    """

    start: time
    end: time


@dataclass(frozen=True)
class DeliveryWindowPolicy:
    """Extensible per-channel quiet windows (v1: WhatsApp 21:00–08:00 local).

    Later: property / tenant / country / holiday overrides without dispatcher changes.
    """

    windows_by_channel: dict[str, ChannelQuietWindow]

    @classmethod
    def default(cls) -> DeliveryWindowPolicy:
        return cls(
            windows_by_channel={
                GuestMessageChannel.WHATSAPP: ChannelQuietWindow(
                    start=time(21, 0),
                    end=time(8, 0),
                ),
            }
        )

    def window_for(self, channel: str) -> ChannelQuietWindow | None:
        return self.windows_by_channel.get(str(channel or "").strip().lower())


def _zoneinfo(iana: str) -> ZoneInfo:
    name = (iana or "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — invalid IANA → UTC
        return ZoneInfo("UTC")


def is_in_quiet_window(local_now: datetime, window: ChannelQuietWindow) -> bool:
    """True when ``local_now``'s local time falls inside the quiet window."""
    t = local_now.timetz().replace(tzinfo=None) if local_now.tzinfo else local_now.time()
    # Compare as naive local clock.
    clock = time(t.hour, t.minute, t.second, t.microsecond)
    start, end = window.start, window.end
    if start == end:
        return False
    if start < end:
        # Same-day window e.g. 01:00–05:00
        return start <= clock < end
    # Wraps midnight e.g. 21:00–08:00
    return clock >= start or clock < end


def next_window_end_local(local_now: datetime, window: ChannelQuietWindow) -> datetime:
    """Next local datetime when the quiet window ends (send allowed).

    For wrap-around 21:00–08:00: if before end same calendar day → today 08:00;
    if at/after start → tomorrow 08:00.
    """
    tz = local_now.tzinfo or ZoneInfo("UTC")
    clock = local_now.timetz().replace(tzinfo=None) if local_now.tzinfo else local_now.time()
    clock = time(clock.hour, clock.minute, clock.second, clock.microsecond)
    start, end = window.start, window.end
    day: date = local_now.date()

    if start < end:
        # Same-day quiet: end is later today, or tomorrow if past end.
        if clock < end:
            target_day = day
        else:
            target_day = day + timedelta(days=1)
    else:
        # Wraps midnight: quiet from start…midnight and midnight…end.
        if clock < end:
            target_day = day
        else:
            # At/after end and before start → not quiet (caller shouldn't ask);
            # at/after start → next calendar day's end.
            target_day = day + timedelta(days=1)

    return datetime.combine(target_day, end, tzinfo=tz)


class DispatchPolicy:
    """Evaluate channel sendability for a dispatch."""

    def __init__(self, window_policy: DeliveryWindowPolicy | None = None) -> None:
        self.window_policy = window_policy or DeliveryWindowPolicy.default()

    def evaluate(
        self,
        dispatch: MessageDispatch,
        channel: str,
        *,
        now: datetime | None = None,
    ) -> PolicyDecision:
        channel_key = str(channel or "").strip().lower()
        iana = (getattr(dispatch, "timezone", None) or "").strip() or "UTC"
        tz = _zoneinfo(iana)
        clock = now or timezone.now()
        if timezone.is_naive(clock):
            clock = timezone.make_aware(clock, timezone=ZoneInfo("UTC"))
        local_now = clock.astimezone(tz)

        window = self.window_policy.window_for(channel_key)
        if window is None:
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW,
                channel=channel_key,
                timezone=iana,
            )

        if not is_in_quiet_window(local_now, window):
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW,
                channel=channel_key,
                timezone=iana,
            )

        next_local = next_window_end_local(local_now, window)
        next_utc = next_local.astimezone(ZoneInfo("UTC"))
        return PolicyDecision(
            kind=PolicyDecisionKind.DEFER,
            reason="quiet_hours",
            next_attempt_at=next_utc,
            timezone=iana,
            channel=channel_key,
        )


# Process-wide default used by the dispatcher.
dispatch_policy = DispatchPolicy()
