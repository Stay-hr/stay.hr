from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.test import SimpleTestCase, TestCase
from lxml import etree

from apps.billing.models import (
    FiscalPreparer,
    ForeignServiceInvoice,
    TaxOffice,
    TenantFiscalSettings,
)
from apps.billing.services.eporezna.errors import PdvBuildError
from apps.billing.services.eporezna.filename import build_filename
from apps.billing.services.eporezna.pdv.builder import NS, PDVBuilder
from apps.billing.services.eporezna.pdv.validate import validate_pdv_xml
from apps.billing.services.eporezna.period import FiscalPeriod
from apps.tenants.models import Tenant

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPECTED_PDV = FIXTURES / "pdv_zaglavlje_zero_expected.xml"

FIXED_UUID = UUID("b847a4af-4a12-476d-85ca-ac495d89360c")
FIXED_NOW = datetime(2026, 7, 28, 9, 39, 37, tzinfo=timezone.utc)


class _FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class _FixedUuid:
    def new(self) -> UUID:
        return FIXED_UUID


class FiscalPeriodTests(SimpleTestCase):
    def test_from_year_month(self):
        period = FiscalPeriod.from_year_month("2026-06")
        self.assertEqual(period.period, "2026-06")
        self.assertEqual(period.date_from, date(2026, 6, 1))
        self.assertEqual(period.date_to, date(2026, 6, 30))
        self.assertEqual(period.filename_range, "20260601-20260630")

    def test_invalid_period(self):
        with self.assertRaises(ValueError):
            FiscalPeriod.from_year_month("2026-13")

    def test_filename_helper(self):
        period = FiscalPeriod.from_year_month("2026-06")
        self.assertEqual(
            build_filename(form="PDV", oib="07155680871", period=period),
            "PDV_07155680871_20260601-20260630.xml",
        )


class PDVBuilderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="PDV Tenant", slug="pdv-t")
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
        ForeignServiceInvoice.objects.create(
            tenant=self.tenant,
            provider=ForeignServiceInvoice.Provider.BOOKING,
            supplier_name="Booking.com B.V.",
            supplier_country="NL",
            supplier_vat_id="805734958B01",
            invoice_number="1657100253",
            invoice_date=date(2026, 7, 3),
            tax_period="2026-06",
            period_from=date(2026, 6, 1),
            period_to=date(2026, 6, 30),
            taxable_amount=Decimal("69.48"),
            currency="EUR",
            document_sha256="a" * 64,
            parsed_payload={},
            imported_at=FIXED_NOW,
        )

    def test_builder_rejects_empty_period(self):
        with self.assertRaises(PdvBuildError) as ctx:
            PDVBuilder(clock=_FixedClock(), uuids=_FixedUuid()).build(
                tenant=self.tenant,
                period="2026-05",
            )
        self.assertIn("No source fiscal data", str(ctx.exception))

    def test_builder_snapshot_xpath_xsd_and_roundtrip(self):
        export = PDVBuilder(clock=_FixedClock(), uuids=_FixedUuid()).build(
            tenant=self.tenant,
            period="2026-06",
        )
        self.assertEqual(
            export.filename,
            "PDV_07155680871_20260601-20260630.xml",
        )
        validate_pdv_xml(export.xml_bytes)
        expected = EXPECTED_PDV.read_bytes()
        self.assertEqual(export.xml_bytes, expected)

        root = etree.fromstring(export.xml_bytes)
        ns = {"p": NS, "m": "http://e-porezna.porezna-uprava.hr/sheme/Metapodaci/v2-0"}
        self.assertEqual(
            root.xpath("string(m:Metapodaci/m:Uskladjenost)", namespaces=ns),
            "ObrazacPDV-v11-0",
        )
        self.assertEqual(
            root.xpath("string(p:Zaglavlje/p:Obveznik/p:OIB)", namespaces=ns),
            "07155680871",
        )
        self.assertEqual(
            root.xpath(
                "string(p:Tijelo/p:Podatak210/p:Vrijednost)",
                namespaces=ns,
            ),
            "0.00",
        )
        self.assertEqual(
            root.xpath("string(p:Tijelo/p:Podatak660)", namespaces=ns),
            "false",
        )

        # Round-trip: parse → serialize must be byte-identical.
        parsed = etree.fromstring(export.xml_bytes)
        roundtrip = etree.tostring(
            parsed,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=False,
        )
        self.assertEqual(roundtrip, export.xml_bytes)
