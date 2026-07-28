from __future__ import annotations

from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.models import FiscalPreparer, TaxOffice, TenantFiscalSettings
from apps.billing.services.eporezna.readiness import fiscal_pdvs_readiness
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
        readiness = fiscal_pdvs_readiness(self.tenant)
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
        self.assertIn("No foreign service invoices", empty_period.json()["detail"])

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

    def test_export_requires_config(self):
        response = self.client.get(
            self.export_url,
            {"period": "2026-06"},
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("missing", response.json())
