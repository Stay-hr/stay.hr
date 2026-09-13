from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.billing.models import Invoice
from apps.core.timezone import effective_timezone


def _tenant_wall_clock(invoice: Invoice, moment: datetime) -> datetime:
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    property = None
    if invoice.reservation_id:
        property = getattr(invoice.reservation, "property", None)
    tz_name = effective_timezone(property=property, tenant=invoice.tenant)
    return moment.astimezone(ZoneInfo(tz_name))


def issued_at_for_f1(invoice: Invoice) -> datetime:
    """Wall-clock used in ZKI and F73 DatVrijeme: tenant/property TZ, not Django UTC."""
    return _tenant_wall_clock(invoice, invoice.issued_at)


def message_at_for_f1(invoice: Invoice) -> datetime:
    """Zaglavlje DatumVrijeme: when Stay submits the CIS message."""
    return _tenant_wall_clock(invoice, timezone.now())


def format_f73_datetime(moment: datetime) -> str:
    return moment.strftime("%d.%m.%YT%H:%M:%S")
