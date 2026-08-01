"""Process-local eVisitor counters (ops / system status)."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

_lock = Lock()
# key: (tenant_slug, property_id, reason) -> count
_checkout_failed_total: dict[tuple[str, str, str], int] = defaultdict(int)
# key: result -> count (complete | partial | none | not_required)
_checkin_auto_total: dict[str, int] = defaultdict(int)

_CHECKIN_AUTO_RESULTS = frozenset({"complete", "partial", "none", "not_required"})


def record_checkout_failed(
    *,
    tenant: str = "",
    property_id: str | int = "",
    reason: str = "api_error",
) -> None:
    key = (str(tenant or ""), str(property_id or ""), str(reason or "api_error"))
    with _lock:
        _checkout_failed_total[key] += 1


def get_evisitor_checkout_failed_total() -> int:
    with _lock:
        return sum(_checkout_failed_total.values())


def get_evisitor_checkout_failed_breakdown() -> list[dict[str, str | int]]:
    with _lock:
        return [
            {
                "tenant": tenant,
                "property_id": property_id,
                "reason": reason,
                "count": count,
            }
            for (tenant, property_id, reason), count in sorted(
                _checkout_failed_total.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]


def reset_evisitor_checkout_failed_total() -> None:
    with _lock:
        _checkout_failed_total.clear()


def record_checkin_auto(*, result: str) -> None:
    key = str(result or "")
    if key not in _CHECKIN_AUTO_RESULTS:
        key = "none"
    with _lock:
        _checkin_auto_total[key] += 1


def get_evisitor_checkin_auto_total() -> int:
    with _lock:
        return sum(_checkin_auto_total.values())


def get_evisitor_checkin_auto_breakdown() -> list[dict[str, str | int]]:
    with _lock:
        return [
            {"result": result, "count": count}
            for result, count in sorted(
                _checkin_auto_total.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]


def reset_evisitor_checkin_auto_total() -> None:
    with _lock:
        _checkin_auto_total.clear()
