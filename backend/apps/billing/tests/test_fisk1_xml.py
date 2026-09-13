from decimal import Decimal
from lxml import etree
from django.test import SimpleTestCase, TestCase

from apps.billing.models import TenantFiscalSettings
from apps.billing.services.fisk1.connector import _sign_xml, _wrap_soap
from apps.billing.services.fisk1.xml_builder import (
    NS,
    build_racun_xml,
    format_cis_http_error,
    parse_cis_errors,
    recipient_oib_for_f1,
)
from apps.billing.tests.helpers import make_test_p12
from apps.tenants.models import Tenant


def _sample_racun(**overrides):
    kwargs = dict(
        oib="91381354893",
        issued_at_iso="13.09.2026T10:00:00",
        sequence_number=262,
        vat_rate=Decimal("13.00"),
        vat_base=Decimal("263.11"),
        vat_amount=Decimal("32.25"),
        total=Decimal("295.36"),
        payment_code="T",
        operator_oib="91381354893",
        zki="a" * 32,
        business_premise_code="ROOMS",
        payment_device_code="1",
        message_id="11111111-1111-1111-1111-111111111111",
        message_at_iso="13.09.2026T10:05:00",
    )
    kwargs.update(overrides)
    return build_racun_xml(**kwargs)


class Fisk1XmlBuilderTests(SimpleTestCase):
    def test_recipient_oib_accepts_only_eleven_digits(self):
        self.assertEqual(recipient_oib_for_f1("87357644223"), "87357644223")
        self.assertEqual(recipient_oib_for_f1("HR 87357644223"), "87357644223")
        self.assertEqual(recipient_oib_for_f1("119907920"), "")
        self.assertEqual(recipient_oib_for_f1(""), "")

    def test_f73_has_zaglavlje_and_nested_brrac(self):
        root = _sample_racun()
        zaglavlje = root.find(f"{{{NS}}}Zaglavlje")
        racun = root.find(f"{{{NS}}}Racun")
        br_rac = racun.find(f"{{{NS}}}BrRac")

        self.assertEqual(NS, "http://www.apis-it.hr/fin/2012/types/f73")
        self.assertEqual(root.get("Id"), "RacunZahtjev")
        self.assertEqual(
            root.tag,
            "{http://www.apis-it.hr/fin/2012/types/f73}RacunZahtjev",
        )
        self.assertEqual(
            zaglavlje.findtext(f"{{{NS}}}IdPoruke"),
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(
            zaglavlje.findtext(f"{{{NS}}}DatumVrijeme"),
            "13.09.2026T10:05:00",
        )
        self.assertEqual(br_rac.findtext(f"{{{NS}}}BrOznRac"), "262")
        self.assertEqual(br_rac.findtext(f"{{{NS}}}OznPosPr"), "ROOMS")
        self.assertEqual(br_rac.findtext(f"{{{NS}}}OznNapUr"), "1")
        self.assertIsNone(racun.find(f"{{{NS}}}OznPosPr"))
        self.assertEqual(racun.findtext(f"{{{NS}}}NakDost"), "false")
        self.assertIsNone(racun.find(f"{{{NS}}}OibPrimateljaRacuna"))
        self.assertEqual(racun.findtext(f"{{{NS}}}IznosUkupno"), "295.36")

    def test_negative_storno_total_and_recipient_oib(self):
        root = _sample_racun(
            sequence_number=261,
            vat_base=Decimal("-248.11"),
            vat_amount=Decimal("-32.25"),
            total=Decimal("-295.36"),
            recipient_oib="87357644223",
            nontaxable_amount=Decimal("-15.00"),
        )
        racun = root.find(f"{{{NS}}}Racun")
        self.assertEqual(racun.findtext(f"{{{NS}}}IznosUkupno"), "-295.36")
        self.assertEqual(racun.findtext(f"{{{NS}}}IznosNePodlOpor"), "-15.00")
        self.assertEqual(
            racun.findtext(f"{{{NS}}}OibPrimateljaRacuna"),
            "87357644223",
        )

    def test_tourist_tax_is_nontaxable_amount(self):
        root = _sample_racun(
            vat_base=Decimal("248.11"),
            nontaxable_amount=Decimal("15.00"),
        )
        racun = root.find(f"{{{NS}}}Racun")
        pdv = racun.find(f"{{{NS}}}Pdv")
        self.assertEqual(pdv.find(f"{{{NS}}}Porez").findtext(f"{{{NS}}}Osnovica"), "248.11")
        self.assertEqual(racun.findtext(f"{{{NS}}}IznosNePodlOpor"), "15.00")
        self.assertEqual(racun.findtext(f"{{{NS}}}IznosUkupno"), "295.36")

    def test_omits_nontaxable_when_zero(self):
        racun = _sample_racun().find(f"{{{NS}}}Racun")
        self.assertIsNone(racun.find(f"{{{NS}}}IznosNePodlOpor"))

    def test_parse_cis_errors_from_soap_fault(self):
        xml = (
            '<soap:Envelope xmlns:tns="http://www.apis-it.hr/fin/2012/types/f73" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soap:Body><tns:Odgovor>"
            "<tns:Greske><tns:Greska>"
            "<tns:SifraGreske>s006</tns:SifraGreske>"
            "<tns:PorukaGreske>Sistemska pogre&#353;ka prilikom obrade zahtjeva.</tns:PorukaGreske>"
            "</tns:Greska></tns:Greske>"
            "</tns:Odgovor></soap:Body></soap:Envelope>"
        )
        self.assertEqual(
            parse_cis_errors(xml),
            [("s006", "Sistemska pogreška prilikom obrade zahtjeva.")],
        )
        self.assertEqual(
            format_cis_http_error(500, xml),
            "CIS HTTP 500: s006 Sistemska pogreška prilikom obrade zahtjeva.",
        )


class CisF1SignTests(TestCase):
    def test_sign_xml_allows_cis_required_sha1(self):
        tenant = Tenant.objects.create(name="Sign Tenant", slug="fisk1-sign")
        settings = TenantFiscalSettings.objects.create(
            tenant=tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer",
            business_premise_code="ROOMS",
            payment_device_code="1",
            certificate_file=make_test_p12(password="secret", oib="12345678901"),
        )
        settings.set_certificate_password("secret")
        settings.save()

        signed = _sign_xml(_sample_racun(), settings)
        tree = etree.fromstring(signed)
        serialized = signed.decode("utf-8")
        signatures = [
            elem for elem in tree.iter() if elem.tag.endswith("Signature")
        ]
        self.assertTrue(signatures)
        self.assertEqual(tree.get("Id"), "RacunZahtjev")
        self.assertNotIn("ds:Signature", serialized)
        self.assertIn('<Signature xmlns="http://www.w3.org/2000/09/xmldsig#">', serialized)
        self.assertIn('URI="#RacunZahtjev"', serialized)
        methods = [
            elem.get("Algorithm", "")
            for elem in tree.iter()
            if elem.tag.endswith("SignatureMethod")
        ]
        self.assertTrue(any("rsa-sha1" in alg for alg in methods))
        c14n = [
            elem.get("Algorithm", "")
            for elem in tree.iter()
            if elem.tag.endswith("CanonicalizationMethod")
        ]
        self.assertTrue(
            any(alg == "http://www.w3.org/2001/10/xml-exc-c14n#" for alg in c14n)
        )


class Fisk1SoapEnvelopeTests(SimpleTestCase):
    def test_wrap_soap_uses_soap11_envelope(self):
        payload = _wrap_soap(b'<tns:RacunZahtjev xmlns:tns="http://www.apis-it.hr/fin/2012/types/f73"/>')
        self.assertTrue(payload.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertIn(
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">',
            payload,
        )
        self.assertIn("<soapenv:Body>", payload)
        self.assertIn("tns:RacunZahtjev", payload)
        self.assertNotIn("<?xml", payload.split("?>", 1)[1])
