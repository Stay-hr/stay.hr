from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.integrations.whatsapp.config import (
    access_token_from_env,
    webhook_verify_signature_from_env,
)
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig


class WhatsAppConfigTests(SimpleTestCase):
    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": "test-token"}, clear=False)
    def test_access_token_from_env(self):
        self.assertEqual(access_token_from_env(), "test-token")

    @patch.dict("os.environ", {}, clear=True)
    def test_webhook_signature_enabled_by_default(self):
        self.assertTrue(webhook_verify_signature_from_env())

    @patch.dict("os.environ", {"WHATSAPP_WEBHOOK_VERIFY_SIGNATURE": "false"}, clear=False)
    def test_webhook_signature_can_be_disabled(self):
        self.assertFalse(webhook_verify_signature_from_env())

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": "test-token"}, clear=False)
    def test_runtime_config_injects_token_from_env(self):
        runtime = WhatsAppRuntimeConfig.from_integration_dict(
            {"phone_number_id": "123", "display_phone_number": "+385911234567"}
        )
        self.assertEqual(runtime.access_token, "test-token")
        self.assertTrue(runtime.send_credentials_ok())

    def test_runtime_config_requires_phone_number_id_for_send(self):
        runtime = WhatsAppRuntimeConfig.from_integration_dict({})
        self.assertFalse(runtime.send_credentials_ok())

    @patch.dict("os.environ", {"WHATSAPP_WABA_ID": "env-waba"}, clear=False)
    def test_effective_waba_id_fallback(self):
        runtime = WhatsAppRuntimeConfig.from_integration_dict({"phone_number_id": "1"})
        self.assertEqual(runtime.effective_waba_id(), "env-waba")
        runtime2 = WhatsAppRuntimeConfig.from_integration_dict(
            {"phone_number_id": "1", "waba_id": "cfg-waba"}
        )
        self.assertEqual(runtime2.effective_waba_id(), "cfg-waba")
