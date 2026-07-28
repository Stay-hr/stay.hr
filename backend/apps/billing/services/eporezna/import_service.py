from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from apps.billing.models import ForeignServiceInvoice
from apps.billing.services.eporezna.dto import ParsedForeignServiceInvoice
from apps.billing.services.eporezna.errors import InvoiceConflictError
from apps.billing.services.eporezna.parsers.booking_pdf import extract_pdf_text
from apps.billing.services.eporezna.parsers.bootstrap import bootstrap_invoice_parsers
from apps.billing.services.eporezna.parsers.registry import invoice_parser_registry
from apps.billing.services.eporezna.validators import ForeignServiceInvoiceValidator
from apps.tenants.models import Tenant


def sha256_bytes(raw: bytes) -> str:
    """Fingerprint original document bytes (not extracted text)."""
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ImportResult:
    invoice: ForeignServiceInvoice | None
    dto: ParsedForeignServiceInvoice
    document_sha256: str
    already_imported: bool
    dry_run: bool


def import_foreign_service_invoice(
    *,
    tenant: Tenant,
    raw: bytes,
    filename: str,
    user=None,
    dry_run: bool = False,
    validator: ForeignServiceInvoiceValidator | None = None,
) -> ImportResult:
    """Parse → validate → upsert under a single atomic transaction (unless dry_run).

    Idempotent on ``document_sha256``: same PDF returns the existing row without error.
    ``parsed_payload`` is write-once and never updated on re-import.
    """
    bootstrap_invoice_parsers()
    validator = validator or ForeignServiceInvoiceValidator()
    digest = sha256_bytes(raw)

    existing = ForeignServiceInvoice.objects.filter(
        tenant=tenant,
        document_sha256=digest,
    ).first()
    if existing is not None:
        dto = _dto_from_model(existing)
        return ImportResult(
            invoice=None if dry_run else existing,
            dto=dto,
            document_sha256=digest,
            already_imported=True,
            dry_run=dry_run,
        )

    try:
        text = extract_pdf_text(raw)
    except Exception:
        text = ""
    parser = invoice_parser_registry.detect(filename=filename, raw=raw, text=text)
    dto = parser.parse(raw)
    validator.validate(dto)

    if dry_run:
        return ImportResult(
            invoice=None,
            dto=dto,
            document_sha256=digest,
            already_imported=False,
            dry_run=True,
        )

    with transaction.atomic():
        # Re-check inside transaction for races
        existing = (
            ForeignServiceInvoice.objects.select_for_update()
            .filter(tenant=tenant, document_sha256=digest)
            .first()
        )
        if existing is not None:
            return ImportResult(
                invoice=existing,
                dto=_dto_from_model(existing),
                document_sha256=digest,
                already_imported=True,
                dry_run=False,
            )

        conflict = ForeignServiceInvoice.objects.filter(
            tenant=tenant,
            provider=dto.provider,
            invoice_number=dto.invoice_number,
        ).first()
        if conflict is not None:
            raise InvoiceConflictError(
                f"Invoice {dto.provider}:{dto.invoice_number} already imported "
                f"with a different document (sha256={conflict.document_sha256[:12]}…)"
            )

        now = datetime.now(timezone.utc)
        invoice = ForeignServiceInvoice(
            tenant=tenant,
            provider=dto.provider,
            supplier_name=dto.supplier_name,
            supplier_country=dto.supplier_country.upper(),
            supplier_vat_id=dto.supplier_vat_id,
            invoice_number=dto.invoice_number,
            invoice_date=dto.invoice_date,
            tax_period=dto.tax_period,
            period_from=dto.period_from,
            period_to=dto.period_to,
            taxable_amount=dto.taxable_amount,
            currency=dto.currency,
            document_sha256=digest,
            parsed_payload=asdict(dto) | {"invoice_date": dto.invoice_date.isoformat(),
                                           "period_from": dto.period_from.isoformat(),
                                           "period_to": dto.period_to.isoformat(),
                                           "taxable_amount": str(dto.taxable_amount)},
            created_by=user if getattr(user, "pk", None) else None,
            updated_by=user if getattr(user, "pk", None) else None,
            imported_at=now,
        )
        invoice.source_document.save(filename, ContentFile(raw), save=False)
        try:
            invoice.save()
        except IntegrityError as exc:
            raise InvoiceConflictError(
                f"Could not import invoice {dto.invoice_number}: {exc}"
            ) from exc

    return ImportResult(
        invoice=invoice,
        dto=dto,
        document_sha256=digest,
        already_imported=False,
        dry_run=False,
    )


def _dto_from_model(inv: ForeignServiceInvoice) -> ParsedForeignServiceInvoice:
    return ParsedForeignServiceInvoice(
        provider=inv.provider,
        supplier_name=inv.supplier_name,
        supplier_country=inv.supplier_country,
        supplier_vat_id=inv.supplier_vat_id,
        invoice_number=inv.invoice_number,
        invoice_date=inv.invoice_date,
        tax_period=inv.tax_period,
        period_from=inv.period_from,
        period_to=inv.period_to,
        taxable_amount=inv.taxable_amount,
        currency=inv.currency,
        raw_fields=dict(inv.parsed_payload or {}),
    )
