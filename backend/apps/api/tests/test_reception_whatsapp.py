from django.test import TestCase, override_settings

from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class ReceptionWhatsAppAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="WA API Tenant", slug="wa-api-tenant")
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Test Tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_integration_status_not_connected(self):
        response = self.client.get("/api/v1/reception/whatsapp/integration/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["connected"])

    @override_settings(META_APP_ID="123", WHATSAPP_APP_SECRET="secret")
    def test_integration_embedded_signup_supported_flag(self):
        response = self.client.get("/api/v1/reception/whatsapp/integration/", **self.auth)
        self.assertTrue(response.json()["embedded_signup_supported"])
