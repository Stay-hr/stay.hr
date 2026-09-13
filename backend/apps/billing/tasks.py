from __future__ import annotations

import logging

from celery import shared_task

from apps.billing.models import Invoice, TenantFiscalSettings
from apps.billing.services.fisk1.connector import (
    Fisk1Connector,
    apply_fiscalization_result,
    record_fiscalization_failure,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def fiscalize_invoice(self, invoice_id: int) -> dict:
    """Submit the invoice to CIS from Stay using the tenant's own F1 certificate."""
    try:
        invoice = Invoice.objects.select_related(
            "tenant",
            "reservation",
            "reservation__property",
        ).get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return {"status": "missing", "invoice_id": invoice_id}

    if invoice.fiscal_status == Invoice.FiscalStatus.FISCALIZED and invoice.jir:
        return {"status": "already_fiscalized", "invoice_id": invoice_id}

    settings = TenantFiscalSettings.objects.filter(tenant_id=invoice.tenant_id).first()
    if settings is None:
        record_fiscalization_failure(
            invoice,
            attempt_no=self.request.retries + 1,
            error_message="Tenant fiscal settings missing.",
        )
        return {"status": "failed", "invoice_id": invoice_id}

    attempt_no = invoice.fiscalization_attempts.count() + 1
    try:
        result = Fisk1Connector().fiscalize(invoice, settings)
        apply_fiscalization_result(invoice, settings, result, attempt_no=attempt_no)
        delivery = {"status": "skipped", "reason": "not_attempted"}
        try:
            from apps.communications.invoice_link_distribute import deliver_invoice_link

            invoice.refresh_from_db()
            delivery = deliver_invoice_link(invoice)
        except Exception:
            logger.exception(
                "invoice link delivery failed invoice_id=%s",
                invoice_id,
            )
            delivery = {"status": "failed", "invoice_id": invoice_id}
        return {
            "status": "fiscalized",
            "invoice_id": invoice_id,
            "jir": result.jir,
            "delivery": delivery,
        }
    except Exception as exc:
        logger.exception("Fiscalization failed invoice_id=%s", invoice_id)
        record_fiscalization_failure(
            invoice,
            attempt_no=attempt_no,
            error_message=str(exc),
            request_snapshot=getattr(exc, "request_snapshot", "") or "",
            response_snapshot=getattr(exc, "response_snapshot", "") or "",
            fiskal_request_id=getattr(exc, "fiskal_request_id", None),
        )
        raise self.retry(exc=exc) from exc


@shared_task
def send_invoice_email_task(invoice_id: int) -> dict:
    from apps.communications.invoice_email import send_invoice_email

    return send_invoice_email(invoice_id)
