import inspect
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.billing.exceptions import InvoiceReplacementError
from apps.billing.models import (
    Invoice,
    InvoiceReplacement,
    InvoiceReplacementRecipient,
    TenantFiscalSettings,
)
from apps.billing.services.invoice_replacement import ReplacementRejectReason
from apps.billing.services import invoice_replacement_service as replacement_commands
from apps.billing.services.invoice_replacement_service import (
    cancel_replacement_case,
    open_replacement_case,
    update_replacement_recipient,
    verify_replacement_recipient,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def _ready_fields() -> dict:
    return {
        "company_name": "PRO AUTOMATIKA",
        "tax_id": "87357644223",
        "tax_id_country": "HR",
        "country": "HR",
        "address": "Novo naselje 19E",
        "postal_code": "22214",
        "city": "Bilice",
        "email": "domagoj.perjanec@pro-automatika.hr",
        "source": "staff",
        "source_excerpt": "R1 request from booking message",
    }


class InvoiceReplacementCommandTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Command Tenant",
            slug="invoice-replacement-commands",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        self.settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="10987654321",
            issuer_name="LIVE SETTINGS d.o.o.",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="DARIO PREZEC",
            amount=Decimal("100.00"),
            buyer_company_name="",
            buyer_oib="",
            buyer_address="",
        )
        self.actor = get_user_model().objects.create_user(
            username="replacement-admin",
            password="x",
            is_staff=True,
        )
        self.other_actor = get_user_model().objects.create_user(
            username="other-admin",
            password="x",
            is_staff=True,
        )
        self.legacy = self._add_invoice(sequence_number=259, invoice_number="259-ROOMS-1")

    def _add_invoice(self, *, sequence_number: int, invoice_number: str | None = None, **overrides) -> Invoice:
        values = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "invoice_number": invoice_number or f"{sequence_number}-ROOMS-1",
            "sequence_number": sequence_number,
            "issued_at": datetime(2026, 5, 27, 7, 21, tzinfo=ZoneInfo("Europe/Zagreb")),
            "buyer_name": "DARIO PREZEC",
            "payment_method": Invoice.PaymentMethod.BOOKING,
            "subtotal": Decimal("88.50"),
            "vat_amount": Decimal("11.50"),
            "total": Decimal("100.00"),
        }
        values.update(overrides)
        return Invoice.objects.create(**values)

    def _open_legacy(self, **kwargs) -> InvoiceReplacement:
        fields = dict(
            original=self.legacy,
            actor=self.actor,
            reason="Wrong buyer on issued invoice",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        fields.update(kwargs)
        return open_replacement_case(**fields)

    def test_commands_lock_reservation_before_case_and_never_write_invoices(self):
        module_source = inspect.getsource(replacement_commands)
        self.assertIn("Reservation.objects.select_for_update", module_source)
        self.assertIn("InvoiceReplacement.objects.select_for_update", module_source)
        self.assertNotIn("Invoice.objects.create", module_source)
        self.assertNotIn("TenantFiscalSettings", module_source)
        for fn, case_lock in (
            (open_replacement_case, "_lock_open_case"),
            (update_replacement_recipient, "_lock_case"),
            (verify_replacement_recipient, "_lock_case"),
            (cancel_replacement_case, "_lock_case"),
        ):
            source = inspect.getsource(fn)
            self.assertIn("Reservation.objects.select_for_update", source)
            self.assertIn(case_lock, source)
            self.assertLess(
                source.index("Reservation.objects.select_for_update"),
                source.index(case_lock),
            )
            self.assertNotIn("Invoice.objects.create", source)
            self.assertNotIn("TenantFiscalSettings", source)

    def test_open_legacy_requires_issuer_oib_evidence_not_live_settings(self):
        with self.assertRaises(InvoiceReplacementError) as ctx:
            open_replacement_case(
                original=self.legacy,
                actor=self.actor,
                reason="Wrong buyer",
            )
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.ISSUER_OIB_EVIDENCE_REQUIRED)
        self.assertEqual(InvoiceReplacement.objects.count(), 0)

        case = self._open_legacy()
        self.assertEqual(case.original_issuer_oib, "12345678901")
        self.assertEqual(case.original_issuer_oib_source, "259-ROOMS-1 PDF header")
        self.assertEqual(case.original_issuer_oib_recorded_by_id, self.actor.pk)
        self.assertNotEqual(case.original_issuer_oib, self.settings.issuer_oib)
        self.assertTrue(hasattr(case, "recipient"))
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.issuer_oib, "")
        self.assertEqual(self.legacy.buyer_name, "DARIO PREZEC")

    def test_open_frozen_uses_invoice_oib_and_rejects_mismatch(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest Guest",
            amount=Decimal("80.00"),
        )
        frozen = Invoice.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("70.80"),
            vat_amount=Decimal("9.20"),
            total=Decimal("80.00"),
            issuer_name="Issuer d.o.o.",
            issuer_oib="12345678901",
            business_premise_code="PP1",
            payment_device_code="1",
            reservation_reference="BK-1",
        )
        with self.assertRaises(InvoiceReplacementError) as ctx:
            open_replacement_case(
                original=frozen,
                actor=self.actor,
                reason="Wrong buyer",
                original_issuer_oib="10987654321",
                original_issuer_oib_source="wrong",
            )
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.ISSUER_OIB_MISMATCH)

        case = open_replacement_case(
            original=frozen,
            actor=self.actor,
            reason="Wrong buyer",
        )
        self.assertEqual(case.original_issuer_oib, "12345678901")
        self.assertEqual(case.original_issuer_oib_source, "invoice.issuer_oib")

    def test_open_is_idempotent_and_does_not_rewrite_audit(self):
        first = self._open_legacy()
        opened_at = first.opened_at
        second = open_replacement_case(
            original=self.legacy,
            actor=self.other_actor,
            reason="A different reason",
            original_issuer_oib="99999999999",
            original_issuer_oib_source="should not write",
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(InvoiceReplacement.objects.count(), 1)
        second.refresh_from_db()
        self.assertEqual(second.reason, "Wrong buyer on issued invoice")
        self.assertEqual(second.opened_by_id, self.actor.pk)
        self.assertEqual(second.opened_at, opened_at)
        self.assertEqual(second.original_issuer_oib, "12345678901")

    def test_open_rejects_completed_original(self):
        case = self._open_legacy()
        storno = self._add_invoice(sequence_number=260)
        replacement = self._add_invoice(sequence_number=261)
        case.status = InvoiceReplacement.Status.COMPLETED
        case.storno_invoice = storno
        case.replacement_invoice = replacement
        case.completed_by = self.actor
        case.completed_at = case.opened_at
        case.save()
        with self.assertRaises(InvoiceReplacementError) as ctx:
            open_replacement_case(
                original=self.legacy,
                actor=self.actor,
                reason="retry",
                original_issuer_oib="12345678901",
                original_issuer_oib_source="259-ROOMS-1 PDF header",
            )
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.NOT_EFFECTIVE_ORIGINAL)

    def test_identity_edit_clears_verified_phone_does_not(self):
        case = self._open_legacy()
        update_replacement_recipient(case=case, fields=_ready_fields())
        recipient = verify_replacement_recipient(case=case, actor=self.actor)
        self.assertEqual(
            recipient.identity_confidence,
            InvoiceReplacementRecipient.IdentityConfidence.VERIFIED,
        )
        verified_at = recipient.verified_at

        recipient = update_replacement_recipient(case=case, fields={"phone": "0911234567"})
        self.assertEqual(
            recipient.identity_confidence,
            InvoiceReplacementRecipient.IdentityConfidence.VERIFIED,
        )
        self.assertEqual(recipient.verified_at, verified_at)
        self.assertEqual(recipient.phone, "0911234567")

        recipient = update_replacement_recipient(case=case, fields={"city": "Šibenik"})
        self.assertEqual(
            recipient.identity_confidence,
            InvoiceReplacementRecipient.IdentityConfidence.UNVERIFIED,
        )
        self.assertIsNone(recipient.verified_by_id)
        self.assertIsNone(recipient.verified_at)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.buyer_name, "DARIO PREZEC")
        self.assertEqual(self.reservation.buyer_company_name, "")

    def test_provenance_is_write_once(self):
        case = self._open_legacy()
        update_replacement_recipient(case=case, fields=_ready_fields())
        with self.assertRaises(InvoiceReplacementError) as ctx:
            update_replacement_recipient(
                case=case,
                fields={"source_excerpt": "changed"},
            )
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.INVALID_RECIPIENT)
        recipient = InvoiceReplacementRecipient.objects.get(case=case)
        self.assertEqual(recipient.source_excerpt, "R1 request from booking message")

    def test_verify_requires_ready_and_is_idempotent(self):
        case = self._open_legacy()
        with self.assertRaises(InvoiceReplacementError) as ctx:
            verify_replacement_recipient(case=case, actor=self.actor)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.RECIPIENT_NOT_READY)

        update_replacement_recipient(case=case, fields=_ready_fields())
        first = verify_replacement_recipient(case=case, actor=self.actor)
        second = verify_replacement_recipient(case=case, actor=self.other_actor)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.verified_by_id, self.actor.pk)

    def test_cancel_before_storno_and_reject_after(self):
        case = self._open_legacy()
        cancelled = cancel_replacement_case(
            case=case,
            actor=self.actor,
            cancel_reason="opened by mistake",
        )
        self.assertEqual(cancelled.status, InvoiceReplacement.Status.CANCELLED)
        self.assertEqual(cancelled.cancel_reason, "opened by mistake")
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 1)

        reopened = self._open_legacy(reason="Open after cancel")
        self.assertNotEqual(reopened.pk, cancelled.pk)
        storno = self._add_invoice(sequence_number=260)
        reopened.storno_invoice = storno
        reopened.save()
        with self.assertRaises(InvoiceReplacementError) as ctx:
            cancel_replacement_case(
                case=reopened,
                actor=self.actor,
                cancel_reason="too late",
            )
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.CANCEL_AFTER_STORNO)
        with self.assertRaises(InvoiceReplacementError) as frozen:
            update_replacement_recipient(case=reopened, fields={"city": "Zagreb"})
        self.assertEqual(frozen.exception.reason, ReplacementRejectReason.RECIPIENT_FROZEN)
