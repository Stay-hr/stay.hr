from __future__ import annotations

from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.models import FiscalPreparer, TaxOffice, TenantFiscalSettings
from apps.billing.services.eporezna.readiness import fiscal_eporezna_readiness
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant

BOOKING_PDF = (
    Path(__file__).resolve().parents[2]
    / "billing"
    / "tests"
    / "fixtures"
    / "booking_commission_invoice.pdf"
)


class EporeznaReceptionApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="PDVS API", slug="pdvs-api")
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}
        self.status_url = "/api/v1/reception/eporezna/status/"
        self.list_url = "/api/v1/reception/eporezna/foreign-service-invoices/"
        self.export_url = "/api/v1/reception/eporezna/pdvs/"
        self.pdv_export_url = "/api/v1/reception/eporezna/pdv/"

    def _configure_fiscal(self):
        preparer = FiscalPreparer.objects.create(
            tenant=self.tenant,
            first_name="ANTE",
            last_name="VRCAN",
            email="avrcanus@gmail.com",
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            issuer_oib="07155680871",
            issuer_first_name="CAROLINA",
            issuer_last_name="PLAZA RODRIGUEZ",
            issuer_place="Srima",
            issuer_street="Srima XVIII",
            issuer_street_number="107",
            tax_office_code=TaxOffice.SIBENIK,
            default_preparer=preparer,
        )

    def test_status_not_configured(self):
        response = self.client.get(self.status_url, **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["configured"])
        self.assertIn("issuer_oib", data["missing"])
        self.assertIn("default_preparer", data["missing"])

    def test_status_configured(self):
        self._configure_fiscal()
        readiness = fiscal_eporezna_readiness(self.tenant)
        self.assertTrue(readiness.configured)
        response = self.client.get(self.status_url, **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])
        self.assertEqual(response.json()["missing"], [])

    def test_list_requires_period(self):
        response = self.client.get(self.list_url, **self.auth)
        self.assertEqual(response.status_code, 400)

    def test_upload_list_export(self):
        self._configure_fiscal()
        raw = BOOKING_PDF.read_bytes()
        upload = SimpleUploadedFile(
            "booking.pdf",
            raw,
            content_type="application/pdf",
        )
        created = self.client.post(
            self.list_url,
            {"file": upload},
            format="multipart",
            **self.auth,
        )
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertTrue(body["created"])
        self.assertFalse(body["already_imported"])
        self.assertEqual(body["invoice_number"], "1657100253")
        self.assertEqual(body["tax_period"], "2026-06")
        invoice_id = body["id"]

        again = self.client.post(
            self.list_url,
            {
                "file": SimpleUploadedFile(
                    "booking.pdf",
                    raw,
                    content_type="application/pdf",
                )
            },
            format="multipart",
            **self.auth,
        )
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["created"])
        self.assertTrue(again.json()["already_imported"])
        self.assertEqual(again.json()["id"], invoice_id)

        listed = self.client.get(
            self.list_url,
            {"period": "2026-06"},
            **self.auth,
        )
        self.assertEqual(listed.status_code, 200)
        data = listed.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["results"]), 1)
        self.assertTrue(data["configured"])

        empty_period = self.client.get(
            self.export_url,
            {"period": "2026-05"},
            **self.auth,
        )
        self.assertEqual(empty_period.status_code, 400)
        self.assertIn("No source fiscal data", empty_period.json()["detail"])

        exported = self.client.get(
            self.export_url,
            {"period": "2026-06"},
            **self.auth,
        )
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported["Content-Type"], "application/xml")
        self.assertIn(
            "PDV-S_07155680871_20260601-20260630.xml",
            exported["Content-Disposition"],
        )
        self.assertTrue(exported.content.startswith(b"<?xml"))
        self.assertIn(b"<Isporuka>", exported.content)
        self.assertIn(b"<IsporukeUkupno>", exported.content)
        self.assertIn(b"69.48", exported.content)
        self.assertIn(b"<KodDrzave>NL</KodDrzave>", exported.content)

        pdv_exported = self.client.get(
            self.pdv_export_url,
            {"period": "2026-06"},
            **self.auth,
        )
        self.assertEqual(pdv_exported.status_code, 200)
        self.assertEqual(pdv_exported["Content-Type"], "application/xml")
        self.assertIn(
            "PDV_07155680871_20260601-20260630.xml",
            pdv_exported["Content-Disposition"],
        )
        self.assertTrue(pdv_exported.content.startswith(b"<?xml"))
        self.assertIn(b"ObrazacPDV", pdv_exported.content)
        self.assertIn(b'verzijaSheme="11.0"', pdv_exported.content)
        self.assertIn(b"<Podatak210>", pdv_exported.content)
        self.assertIn(b"<Vrijednost>0.00</Vrijednost>", pdv_exported.content)

    def test_export_requires_config(self):
        response = self.client.get(
            self.export_url,
            {"period": "2026-06"},
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("missing", response.json())

    def test_pdv_and_pdvs_export_from_invoice_row(self):
        from datetime import date, datetime, timezone
        from decimal import Decimal

        from apps.billing.models import ForeignServiceInvoice

        self._configure_fiscal()
        ForeignServiceInvoice.objects.create(
            tenant=self.tenant,
            provider=ForeignServiceInvoice.Provider.BOOKING,
            supplier_name="Booking.com B.V.",
            supplier_country="NL",
            supplier_vat_id="805734958B01",
            invoice_number="api-row-1",
            invoice_date=date(2026, 7, 3),
            tax_period="2026-06",
            period_from=date(2026, 6, 1),
            period_to=date(2026, 6, 30),
            taxable_amount=Decimal("69.48"),
            currency="EUR",
            document_sha256="c" * 64,
            parsed_payload={},
            imported_at=datetime(2026, 7, 28, 9, 39, 37, tzinfo=timezone.utc),
        )

        empty = self.client.get(
            self.pdv_export_url,
            {"period": "2026-05"},
            **self.auth,
        )
        self.assertEqual(empty.status_code, 400)
        self.assertIn("No source fiscal data", empty.json()["detail"])

        pdvs = self.client.get(self.export_url, {"period": "2026-06"}, **self.auth)
        self.assertEqual(pdvs.status_code, 200)
        self.assertIn(
            "PDV-S_07155680871_20260601-20260630.xml",
            pdvs["Content-Disposition"],
        )
        self.assertIn(b"<Isporuka>", pdvs.content)

        pdv = self.client.get(self.pdv_export_url, {"period": "2026-06"}, **self.auth)
        self.assertEqual(pdv.status_code, 200)
        self.assertEqual(pdv["Content-Type"], "application/xml")
        self.assertIn(
            "PDV_07155680871_20260601-20260630.xml",
            pdv["Content-Disposition"],
        )
        self.assertIn(b"ObrazacPDV", pdv.content)
        self.assertIn(b'verzijaSheme="11.0"', pdv.content)
        self.assertIn(b"<Podatak210>", pdv.content)
        self.assertIn(b"<Vrijednost>0.00</Vrijednost>", pdv.content)
