from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.fisk1.cis_tls import CIS_CA_BUNDLE_PATH, cis_verify_path
from apps.billing.services.fisk1.connector import Fisk1Connector
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

EXPECTED_SHA256 = {
    "Fina Demo Root CA": "a0628e66bcfc6ded7b6d8457df57ad5477ee55b7d6e1161e25212bd5b1b1e14d",
    "Fina Demo CA 2020": "9771586d69dca73d70e196b7aa059def3d47b2de4bc080e738e5b1bc8a105bd2",
    "Fina Root CA": "5ab4fcdb180b5b6af0d262a2375a2c77d25602015d96648756611e2e78c53ad3",
    "Fina RDC 2020": "4140b70629fda4b8a36fd53fb0aa53237157869931b8b2308fd05df3ff7d78ab",
}


def _bundle_certs() -> list[x509.Certificate]:
    pem = Path(cis_verify_path()).read_bytes()
    return x509.load_pem_x509_certificates(pem)


class Fisk1CisTlsBundleTests(SimpleTestCase):
    def test_bundle_contains_official_fina_cas(self):
        self.assertTrue(CIS_CA_BUNDLE_PATH.is_file())
        certs = _bundle_certs()
        names = {
            cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
            for cert in certs
        }
        self.assertEqual(set(EXPECTED_SHA256), names)
        fingerprints = {
            cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value: sha256(
                cert.public_bytes(Encoding.DER)
            ).hexdigest()
            for cert in certs
        }
        self.assertEqual(fingerprints, EXPECTED_SHA256)


class Fisk1CisTlsConnectorTests(TestCase):
    @patch("apps.billing.services.fisk1.connector._sign_xml", return_value=b"<signed/>")
    @patch("apps.billing.services.fisk1.connector.httpx.Client")
    def test_default_client_verifies_with_fina_bundle(self, mock_client_cls, _sign_xml):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = (
            '<tns:RacunOdgovor xmlns:tns="http://www.apis-it.hr/fin/2012/types/F73">'
            "<tns:Jir>TLS-JIR</tns:Jir>"
            "</tns:RacunOdgovor>"
        )
        mock_client.post.return_value = mock_response

        tenant = Tenant.objects.create(name="CIS TLS", slug="cis-tls")
        settings = TenantFiscalSettings.objects.create(
            tenant=tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Test",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        reservation = Reservation.objects.create(
            tenant=tenant,
            property=Property.objects.create(tenant=tenant, name="P", slug="p"),
            check_in=datetime(2026, 4, 15).date(),
            check_out=datetime(2026, 4, 16).date(),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest",
            amount=Decimal("100.00"),
        )
        invoice = Invoice.objects.create(
            tenant=tenant,
            reservation=reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 4, 16, 10, 0, 0),
            buyer_name="Guest",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            zki="abc123",
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            sort_order=1,
            line_kind=InvoiceLine.LineKind.ACCOMMODATION,
            description="Noćenje",
            quantity=Decimal("1"),
            unit_price=Decimal("88.50"),
            vat_rate=Decimal("13.00"),
            vat_amount=Decimal("11.50"),
            line_total=Decimal("100.00"),
        )

        result = Fisk1Connector().fiscalize(invoice, settings)
        self.assertEqual(result.jir, "TLS-JIR")
        mock_client_cls.assert_called_once()
        self.assertEqual(mock_client_cls.call_args.kwargs["verify"], cis_verify_path())
        mock_client.close.assert_called_once()
