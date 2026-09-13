from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.billing.exceptions import FiscalizationError
from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.fisk1.connector import SOAP_ACTION, Fisk1Connector, _wrap_soap
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class Fisk1ConnectorTests(TestCase):
    @patch("apps.billing.services.fisk1.connector.httpx.Client")
    @patch("apps.billing.services.fisk1.connector._sign_xml", return_value=b"<signed/>")
    @patch("apps.billing.services.fisk1.connector.pkcs12.load_key_and_certificates")
    def test_fiscalize_parses_jir(self, load_p12, _sign_xml, mock_client_cls):
        load_p12.return_value = (object(), object(), [])
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = (
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soap:Body>"
            '<tns:RacunOdgovor xmlns:tns="http://www.apis-it.hr/fin/2012/types/f73">'
            "<tns:Jir>ABC-DEF-123</tns:Jir>"
            "</tns:RacunOdgovor>"
            "</soap:Body>"
            "</soap:Envelope>"
        )
        mock_client.post.return_value = mock_response

        tenant = Tenant.objects.create(name="Fisk Tenant", slug="fisk")
        settings = TenantFiscalSettings.objects.create(
            tenant=tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Test",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        settings.set_certificate_password("secret")
        settings.save()

        reservation = Reservation.objects.create(
            tenant=tenant,
            property=Property.objects.create(
                tenant=tenant,
                name="P",
                slug="p",
            ),
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
        InvoiceLine.objects.create(
            invoice=invoice,
            sort_order=2,
            line_kind=InvoiceLine.LineKind.TOURIST_TAX_ADULT,
            description="Turistička pristojba",
            quantity=Decimal("1"),
            unit_price=Decimal("15.00"),
            vat_rate=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            line_total=Decimal("15.00"),
        )

        cert_file = MagicMock()
        cert_file.read.return_value = b"fake-p12"
        settings.certificate_file = cert_file

        invoice.buyer_document_number = "87357644223"
        invoice.save(update_fields=["buyer_document_number"])

        connector = Fisk1Connector(http_client=mock_client)
        result = connector.fiscalize(invoice, settings)
        self.assertEqual(result.jir, "ABC-DEF-123")
        mock_client.post.assert_called_once()
        posted = mock_client.post.call_args
        self.assertEqual(
            posted.kwargs["headers"]["SOAPAction"],
            f'"{SOAP_ACTION}"',
        )
        self.assertTrue(
            SOAP_ACTION.startswith(
                "http://e-porezna.porezna-uprava.hr/fiskalizacija/2012/services/"
            )
        )
        payload = posted.kwargs["content"].decode("utf-8")
        self.assertIn("soapenv:Envelope", payload)
        self.assertIn('xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"', payload)
        self.assertIn("<signed/>", payload)

        root = _sign_xml.call_args[0][0]
        ns = "http://www.apis-it.hr/fin/2012/types/f73"
        racun = root.find(f"{{{ns}}}Racun")
        br_rac = racun.find(f"{{{ns}}}BrRac")
        self.assertIsNotNone(root.find(f"{{{ns}}}Zaglavlje"))
        self.assertEqual(br_rac.findtext(f"{{{ns}}}BrOznRac"), "1")
        self.assertEqual(br_rac.findtext(f"{{{ns}}}OznPosPr"), "PP1")
        self.assertEqual(racun.findtext(f"{{{ns}}}NakDost"), "false")
        self.assertEqual(racun.findtext(f"{{{ns}}}OibPrimateljaRacuna"), "87357644223")
        self.assertEqual(racun.findtext(f"{{{ns}}}IznosNePodlOpor"), "15.00")

    @patch("apps.billing.services.fisk1.connector._sign_xml", return_value=b"<signed/>")
    def test_http_error_keeps_parsed_cis_code(self, _sign_xml):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = (
            '<soap:Envelope xmlns:tns="http://www.apis-it.hr/fin/2012/types/f73" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soap:Body><tns:Odgovor><tns:Greske><tns:Greska>"
            "<tns:SifraGreske>s006</tns:SifraGreske>"
            "<tns:PorukaGreske>Sistemska pogreška prilikom obrade zahtjeva.</tns:PorukaGreske>"
            "</tns:Greska></tns:Greske></tns:Odgovor></soap:Body></soap:Envelope>"
        )
        mock_client.post.return_value = mock_response

        tenant = Tenant.objects.create(name="CIS Err", slug="cis-err")
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
            property=Property.objects.create(tenant=tenant, name="P", slug="p-err"),
            check_in=datetime(2026, 4, 15).date(),
            check_out=datetime(2026, 4, 16).date(),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest",
            amount=Decimal("100.00"),
        )
        invoice = Invoice.objects.create(
            tenant=tenant,
            reservation=reservation,
            invoice_number="2-PP1-1",
            sequence_number=2,
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

        with self.assertRaises(FiscalizationError) as ctx:
            Fisk1Connector(http_client=mock_client).fiscalize(invoice, settings)
        self.assertIn("s006", str(ctx.exception))
        self.assertIn("Greske", ctx.exception.response_snapshot)
