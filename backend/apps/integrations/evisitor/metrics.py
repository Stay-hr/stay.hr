"""Process-local eVisitor checkout failure counters (ops / system status)."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

_lock = Lock()
# key: (tenant_slug, property_id, reason) -> count
_checkout_failed_total: dict[tuple[str, str, str], int] = defaultdict(int)


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
