from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.test import TestCase, SimpleTestCase
from lxml import etree

from apps.billing.models import (
    FiscalPreparer,
    ForeignServiceInvoice,
    TaxOffice,
    TenantFiscalSettings,
)
from apps.billing.services.eporezna.dto import ParsedForeignServiceInvoice
from apps.billing.services.eporezna.errors import (
    InvoiceValidationError,
    PdvsBuildError,
    UnknownParserError,
)
from apps.billing.services.eporezna.import_service import (
    import_foreign_service_invoice,
    sha256_bytes,
)
from apps.billing.services.eporezna.parsers.booking_pdf import BookingPdfParser
from apps.billing.services.eporezna.parsers.bootstrap import bootstrap_invoice_parsers
from apps.billing.services.eporezna.parsers.registry import invoice_parser_registry
from apps.billing.services.eporezna.pdvs.builder import NS, PDVSBuilder
from apps.billing.services.eporezna.pdvs.line_mapper import (
    map_invoices_to_pdvs_lines,
    normalize_country_code,
    normalize_vat_id,
)
from apps.billing.services.eporezna.pdvs.validate import validate_pdvs_xml
from apps.billing.services.eporezna.validators import ForeignServiceInvoiceValidator
from apps.tenants.models import Tenant

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BOOKING_PDF = FIXTURES / "booking_commission_invoice.pdf"
EXPECTED_PDVS = FIXTURES / "pdvs_with_isporuke_expected.xml"

FIXED_UUID = UUID("b847a4af-4a12-476d-85ca-ac495d89360c")
FIXED_NOW = datetime(2026, 7, 28, 9, 39, 37, tzinfo=timezone.utc)


class _FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class _FixedUuid:
    def new(self) -> UUID:
        return FIXED_UUID


def _make_invoice(tenant, *, number: str, amount: str, country="NL", vat="805734958B01"):
    return ForeignServiceInvoice.objects.create(
        tenant=tenant,
        provider=ForeignServiceInvoice.Provider.BOOKING,
        supplier_name="Booking.com B.V.",
        supplier_country=country,
        supplier_vat_id=vat,
        invoice_number=number,
        invoice_date=date(2026, 7, 3),
        tax_period="2026-06",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        taxable_amount=Decimal(amount),
        currency="EUR",
        document_sha256=f"{number}-sha256".ljust(64, "0")[:64],
        parsed_payload={},
        imported_at=FIXED_NOW,
    )


class BookingPdfParserTests(SimpleTestCase):
    def test_parse_fixture(self):
        raw = BOOKING_PDF.read_bytes()
        self.assertTrue(
            BookingPdfParser.can_parse(filename=BOOKING_PDF.name, raw=raw, text=None)
        )
        dto = BookingPdfParser().parse(raw)
        self.assertEqual(dto.provider, "booking")
        self.assertEqual(dto.supplier_name, "Booking.com B.V.")
        self.assertEqual(dto.supplier_country, "NL")
        self.assertEqual(dto.supplier_vat_id, "805734958B01")
        self.assertEqual(dto.invoice_number, "1657100253")
        self.assertEqual(dto.invoice_date, date(2026, 7, 3))
        self.assertEqual(dto.tax_period, "2026-06")
        self.assertEqual(dto.period_from, date(2026, 6, 1))
        self.assertEqual(dto.period_to, date(2026, 6, 30))
        self.assertEqual(dto.taxable_amount, Decimal("69.48"))
        self.assertEqual(dto.currency, "EUR")

    def test_sha256_is_over_raw_bytes(self):
        raw = BOOKING_PDF.read_bytes()
        self.assertEqual(sha256_bytes(raw), sha256_bytes(raw))
        self.assertNotEqual(sha256_bytes(raw), sha256_bytes(raw + b"x"))


class ValidatorTests(SimpleTestCase):
    def _valid_dto(self, **overrides) -> ParsedForeignServiceInvoice:
        base = dict(
            provider="booking",
            supplier_name="Booking.com B.V.",
            supplier_country="NL",
            supplier_vat_id="805734958B01",
            invoice_number="1",
            invoice_date=date(2026, 7, 3),
            tax_period="2026-06",
            period_from=date(2026, 6, 1),
            period_to=date(2026, 6, 30),
            taxable_amount=Decimal("10.00"),
            currency="EUR",
        )
        base.update(overrides)
        return ParsedForeignServiceInvoice(**base)

    def test_amount_must_be_positive(self):
        with self.assertRaises(InvoiceValidationError):
            ForeignServiceInvoiceValidator().validate(
                self._valid_dto(taxable_amount=Decimal("0"))
            )

    def test_vat_required(self):
        with self.assertRaises(InvoiceValidationError):
            ForeignServiceInvoiceValidator().validate(self._valid_dto(supplier_vat_id=""))


class RegistryBootstrapTests(SimpleTestCase):
    def test_booking_parser_registered(self):
        bootstrap_invoice_parsers()
        self.assertIn("booking_pdf_v1", invoice_parser_registry.names())


class LineMapperTests(SimpleTestCase):
    def test_normalize_country_and_vat(self):
        self.assertEqual(normalize_country_code(" nl "), "NL")
        self.assertEqual(
            normalize_vat_id("NL805734958B01", country_code="NL"),
            "805734958B01",
        )
        self.assertEqual(normalize_vat_id("nl805734958B01"), "805734958B01")
        self.assertEqual(
            normalize_vat_id("805734958B01", country_code="NL"),
            "805734958B01",
        )


class LineMapperDbTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Mapper", slug="mapper-t")

    def test_aggregate_same_vat(self):
        a = _make_invoice(self.tenant, number="1", amount="10.00")
        b = _make_invoice(self.tenant, number="2", amount="20.50")
        lines = map_invoices_to_pdvs_lines([a, b])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].country_code, "NL")
        self.assertEqual(lines[0].vat_id, "805734958B01")
        self.assertEqual(lines[0].goods_amount, Decimal("0.00"))
        self.assertEqual(lines[0].services_amount, Decimal("30.50"))

    def test_stable_order_two_suppliers(self):
        ie = _make_invoice(
            self.tenant, number="ie1", amount="5.00", country="IE", vat="1234567WH"
        )
        nl = _make_invoice(self.tenant, number="nl1", amount="1.00")
        lines = map_invoices_to_pdvs_lines([nl, ie])
        self.assertEqual([line.country_code for line in lines], ["IE", "NL"])


class ForeignServiceInvoiceImportTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="PDVS Tenant", slug="pdvs-t")
        self.raw = BOOKING_PDF.read_bytes()

    def test_import_and_idempotent_reimport(self):
        first = import_foreign_service_invoice(
            tenant=self.tenant,
            raw=self.raw,
            filename="booking.pdf",
        )
        self.assertFalse(first.already_imported)
        self.assertIsNotNone(first.invoice)
        self.assertEqual(first.dto.taxable_amount, Decimal("69.48"))
        self.assertEqual(ForeignServiceInvoice.objects.count(), 1)
        payload = dict(first.invoice.parsed_payload)

        second = import_foreign_service_invoice(
            tenant=self.tenant,
            raw=self.raw,
            filename="booking-again.pdf",
        )
        self.assertTrue(second.already_imported)
        self.assertEqual(second.invoice.pk, first.invoice.pk)
        self.assertEqual(ForeignServiceInvoice.objects.count(), 1)
        first.invoice.refresh_from_db()
        self.assertEqual(first.invoice.parsed_payload, payload)

    def test_dry_run_does_not_persist(self):
        result = import_foreign_service_invoice(
            tenant=self.tenant,
            raw=self.raw,
            filename="booking.pdf",
            dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.invoice)
        self.assertEqual(ForeignServiceInvoice.objects.count(), 0)
        self.assertEqual(result.dto.invoice_number, "1657100253")

    def test_unknown_pdf_raises(self):
        with self.assertRaises(UnknownParserError):
            import_foreign_service_invoice(
                tenant=self.tenant,
                raw=b"%PDF-1.4 not a booking invoice",
                filename="other.pdf",
            )


class PDVSBuilderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Carolina", slug="carolina")
        preparer = FiscalPreparer.objects.create(
            tenant=self.tenant,
            first_name="ANTE",
            last_name="VRCAN",
            email="avrcanus@gmail.com",
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            issuer_oib="07155680871",
            issuer_name="CAROLINA PLAZA RODRIGUEZ",
            issuer_first_name="CAROLINA",
            issuer_last_name="PLAZA RODRIGUEZ",
            issuer_place="Srima",
            issuer_street="Srima XVIII",
            issuer_street_number="107",
            tax_office_code=TaxOffice.SIBENIK,
            default_preparer=preparer,
        )
        _make_invoice(self.tenant, number="1657100253", amount="69.48")

    def test_builder_rejects_empty_period(self):
        with self.assertRaises(PdvsBuildError) as ctx:
            PDVSBuilder(clock=_FixedClock(), uuids=_FixedUuid()).build(
                tenant=self.tenant,
                period="2026-05",
            )
        self.assertIn("No source fiscal data", str(ctx.exception))

    def test_builder_snapshot_xpath_and_xsd(self):
        export = PDVSBuilder(clock=_FixedClock(), uuids=_FixedUuid()).build(
            tenant=self.tenant,
            period="2026-06",
        )
        self.assertEqual(
            export.filename,
            "PDV-S_07155680871_20260601-20260630.xml",
        )
        validate_pdvs_xml(export.xml_bytes)
        expected = EXPECTED_PDVS.read_bytes()
        self.assertEqual(export.xml_bytes, expected)

        root = etree.fromstring(export.xml_bytes)
        ns = {"p": NS}
        self.assertEqual(
            root.xpath("string(p:Tijelo/p:Isporuke/p:Isporuka[1]/p:KodDrzave)", namespaces=ns),
            "NL",
        )
        self.assertEqual(
            root.xpath("string(p:Tijelo/p:IsporukeUkupno/p:I2)", namespaces=ns),
            "69.48",
        )
        self.assertEqual(
            root.xpath("string(p:Tijelo/p:Isporuke/p:Isporuka[1]/p:PDVID)", namespaces=ns),
            "805734958B01",
        )

    def test_builder_deterministic_except_clock_uuid(self):
        a = PDVSBuilder(clock=_FixedClock(), uuids=_FixedUuid()).build(
            tenant=self.tenant, period="2026-06"
        )
        b = PDVSBuilder(clock=_FixedClock(), uuids=_FixedUuid()).build(
            tenant=self.tenant, period="2026-06"
        )
        self.assertEqual(a.xml_bytes, b.xml_bytes)
