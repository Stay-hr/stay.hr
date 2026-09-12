from django.core.management.base import BaseCommand, CommandError

from apps.billing.exceptions import InvoiceIssuerContextMissing
from apps.billing.models import Invoice
from apps.billing.services.invoice_resolution import resolve_effective_invoice
from apps.billing.services.issue import get_fiscal_settings_for_reservation, refresh_invoice_buyer_from_reservation
from apps.billing.services.issuer_context import require_frozen_issuer_context
from apps.billing.services.pdf import render_invoice_pdf
from apps.reservations.models import Reservation


class Command(BaseCommand):
    help = "Regenerate invoice PDF (optionally refresh buyer snapshot from reservation)."

    def add_arguments(self, parser):
        parser.add_argument("--reservation-id", type=int, help="Reservation ID")
        parser.add_argument("--invoice-id", type=int, help="Invoice ID")

    def handle(self, *args, **options):
        reservation_id = options.get("reservation_id")
        invoice_id = options.get("invoice_id")
        if not reservation_id and not invoice_id:
            raise CommandError("Provide --reservation-id or --invoice-id.")

        if invoice_id:
            invoice = Invoice.objects.select_related("tenant", "reservation").filter(pk=invoice_id).first()
        else:
            reservation = Reservation.objects.filter(pk=reservation_id).first()
            if reservation is None:
                raise CommandError(f"Reservation {reservation_id} not found.")
            invoice = resolve_effective_invoice(reservation)

        if invoice is None:
            raise CommandError("Invoice not found.")

        try:
            require_frozen_issuer_context(invoice)
        except InvoiceIssuerContextMissing as exc:
            raise CommandError(str(exc)) from exc

        refresh_invoice_buyer_from_reservation(invoice)
        settings = get_fiscal_settings_for_reservation(invoice.reservation)
        render_invoice_pdf(invoice, settings)
        self.stdout.write(
            self.style.SUCCESS(
                f"Regenerated PDF for invoice {invoice.invoice_number} (reservation {invoice.reservation_id})."
            )
        )
