"""Overbooking, multi-room gaps, and Channex ARI verify (read-only)."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from apps.core.daily_ops_report.types import MetricResult, ReportSection, Severity, max_severity
from apps.reservations.multi_room_guard import find_all_multi_room_gaps
from apps.reservations.overbooking import find_conflicts
from apps.tenants.models import Tenant


class OverbookingCollector:
    title = "Overbooking + multi-room"

    def collect(self) -> ReportSection:
        tenant_id = int(getattr(settings, "DAILY_OPS_REPORT_TENANT_ID", 2))
        today = timezone.localdate()

        tenant = Tenant.objects.filter(pk=tenant_id).first()
        if tenant is None:
            rows = [
                MetricResult(
                    key="overbooking.conflict_count",
                    value=None,
                    status=Severity.WARN,
                    display=f"tenant {tenant_id} not found",
                ),
                MetricResult(
                    key="multi_room.gap_count",
                    value=None,
                    status=Severity.WARN,
                    display=f"tenant {tenant_id} not found",
                ),
                MetricResult(
                    key="channex.ari_mismatch_count",
                    value=None,
                    status=Severity.WARN,
                    display=f"tenant {tenant_id} not found",
                ),
            ]
            return ReportSection(
                title=self.title,
                severity=Severity.WARN,
                rows=rows,
                summary=f"Tenant id={tenant_id} missing.",
            )

        conflicts = find_conflicts(tenant=tenant, from_date=today)
        gaps = find_all_multi_room_gaps(tenant=tenant, from_date=today)
        conflict_count = len(conflicts)
        gap_count = len(gaps)

        conflict_status = Severity.WARN if conflict_count > 0 else Severity.OK
        gap_status = Severity.WARN if gap_count > 0 else Severity.OK

        ari_metric = self._ari_mismatch_metric(tenant)

        rows = [
            MetricResult(
                key="overbooking.conflict_count",
                value=conflict_count,
                status=conflict_status,
                display=str(conflict_count),
            ),
            MetricResult(
                key="multi_room.gap_count",
                value=gap_count,
                status=gap_status,
                display=str(gap_count),
            ),
            ari_metric,
        ]

        summary = f"Tenant {tenant.slug} (id={tenant_id}), from_date={today.isoformat()}."
        return ReportSection(
            title=self.title,
            severity=max_severity(*(row.status for row in rows)),
            rows=rows,
            summary=summary,
        )

    @staticmethod
    def _ari_mismatch_metric(tenant: Tenant) -> MetricResult:
        """Read-only Channex GET verify for DAILY_OPS_REPORT_TENANT_ID (default 2)."""
        from apps.integrations.channex.availability_verify_service import (
            DEFAULT_VERIFY_DAYS,
            verify_availability,
        )

        slug = (tenant.slug or "").strip()
        if not slug:
            return MetricResult(
                key="channex.ari_mismatch_count",
                value=None,
                status=Severity.WARN,
                display="tenant_slug_missing",
            )

        result = verify_availability(
            tenant_slug=slug,
            days=DEFAULT_VERIFY_DAYS,
            notify=False,
        )
        if result.get("skipped"):
            return MetricResult(
                key="channex.ari_mismatch_count",
                value=None,
                status=Severity.WARN,
                display=str(result.get("reason") or "skipped"),
            )
        mismatch_count = int(result.get("mismatch_count") or 0)
        return MetricResult(
            key="channex.ari_mismatch_count",
            value=mismatch_count,
            status=Severity.WARN if mismatch_count > 0 else Severity.OK,
            display=str(mismatch_count),
        )
