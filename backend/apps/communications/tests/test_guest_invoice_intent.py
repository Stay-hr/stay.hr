from unittest.mock import patch

from django.test import SimpleTestCase

from apps.communications.guest_invoice_intent import (
    classify_invoice_request,
    classify_invoice_request_heuristic,
)


class GuestInvoiceIntentHeuristicTests(SimpleTestCase):
    def test_consumer_plain_invoice(self):
        result = classify_invoice_request_heuristic("Please send invoice")
        self.assertEqual(result.buyer_kind, "consumer")
        self.assertFalse(result.extracted_fields())

    def test_1172_company_and_si_vat(self):
        text = (
            "Poštovani, molim da mi se račun pošalje na e-adresu kada se odjavim. Hvala!\n"
            "Ime tvrtke ovog gosta je Julianna PihlarS.P.\n"
            "PDV identifikacijski broj tvrtke ovog gosta je SI96977604"
        )
        result = classify_invoice_request_heuristic(text)
        self.assertEqual(result.buyer_kind, "business")
        self.assertIn("Julianna", result.company_name)
        self.assertEqual(result.tax_id, "96977604")
        self.assertEqual(result.tax_id_country, "SI")
        self.assertEqual(result.country, "SI")

    def test_hr_oib_labeled(self):
        result = classify_invoice_request_heuristic(
            "Molim račun na firmu PRO AUTOMATIKA OIB 87357644223"
        )
        self.assertEqual(result.buyer_kind, "business")
        self.assertEqual(result.tax_id, "87357644223")
        self.assertEqual(result.tax_id_country, "HR")

    @patch("apps.communications.guest_invoice_intent.llm_configured", return_value=False)
    def test_classify_uses_heuristic_when_llm_off(self, _mock):
        result = classify_invoice_request("Račun molim")
        self.assertEqual(result.buyer_kind, "consumer")
        self.assertEqual(result.source, "heuristic")

    @patch("apps.communications.guest_invoice_intent._running_django_tests", return_value=False)
    @patch("apps.communications.guest_invoice_intent.complete_chat_json")
    @patch("apps.communications.guest_invoice_intent.llm_configured", return_value=True)
    def test_llm_overrides_kind_and_keeps_fallback_on_empty(self, _cfg, mock_llm, _tests):
        mock_llm.return_value = {
            "buyer_kind": "business",
            "company_name": "Example d.o.o.",
            "tax_id": "12345678901",
            "tax_id_country": "HR",
            "country": "HR",
            "address": "",
            "city": "",
            "postal_code": "",
            "email": "acc@example.com",
            "excerpt": "račun na firmu",
        }
        result = classify_invoice_request("Please send invoice to our company")
        self.assertEqual(result.source, "llm")
        self.assertEqual(result.buyer_kind, "business")
        self.assertEqual(result.company_name, "Example d.o.o.")
        self.assertEqual(result.email, "acc@example.com")

    @patch("apps.communications.guest_invoice_intent._running_django_tests", return_value=False)
    @patch("apps.communications.guest_invoice_intent.complete_chat_json", side_effect=Exception("boom"))
    @patch("apps.communications.guest_invoice_intent.llm_configured", return_value=True)
    def test_llm_failure_falls_back(self, _cfg, _llm, _tests):
        result = classify_invoice_request("Please send invoice")
        self.assertEqual(result.source, "heuristic")
        self.assertEqual(result.buyer_kind, "consumer")
