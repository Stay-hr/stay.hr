import hashlib
import hmac
import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_message_timeline import serialize_whatsapp
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.webhook_views import WhatsAppWebhookView
from apps.properties.models import Property
from apps.reservations.models import Guest, Reservation, ReservationVersion, ReservationVersionScope
from apps.tenants.models import Tenant

TEST_FERNET_KEY = "M8U_DJpQILQrKpxTOVtRrQp3nR0LJHAl2X0x-7JOH5k="
TEST_VERIFY_TOKEN = "stay-whatsapp-verify-token"
TEST_APP_SECRET = "whatsapp-test-app-secret"


def _signed_post(factory: RequestFactory, url: str, payload: dict):
    raw = json.dumps(payload).encode("utf-8")
    signature = (
        "sha256="
        + hmac.new(TEST_APP_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    )
    return factory.post(
        url,
        data=raw,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
    )


def _echo_payload(
    *,
    phone_number_id: str,
    guest_wa_id: str,
    wamid: str,
    body: str = "hello from business app",
    message_type: str = "text",
    extra_echo_fields: dict | None = None,
) -> dict:
    echo: dict = {
        "from": "385911111111",
        "to": guest_wa_id,
        "id": wamid,
        "timestamp": "1710000000",
        "type": message_type,
    }
    if message_type == "text":
        echo["text"] = {"body": body}
    elif message_type == "sticker":
        echo["sticker"] = {"id": "sticker-media-id", "animated": False}
    if extra_echo_fields:
        echo.update(extra_echo_fields)
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-ID",
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "385911111111",
                                "phone_number_id": phone_number_id,
                            },
                            "message_echoes": [echo],
                        },
                    }
                ],
            }
        ],
    }


@override_settings(
    ROOT_URLCONF="config.urls",
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class WhatsAppSmbMessageEchoTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        os.environ["WHATSAPP_WEBHOOK_VERIFY_TOKEN"] = TEST_VERIFY_TOKEN
        os.environ["WHATSAPP_APP_SECRET"] = TEST_APP_SECRET

        self.tenant = Tenant.objects.create(
            slug="echo-hotel",
            name="Echo Hotel",
            default_language="hr",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="echo-prop",
            name="Echo Property",
            language="hr",
        )
        self.phone_number_id = "7794189252778687"
        self.guest_wa_id = "385911111111"
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key=self.phone_number_id,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": self.phone_number_id,
                "display_phone_number": "+385911111111",
                "waba_id": "215589313241560883",
                "access_token": "echo-token",
                "auto_reply": False,
            }
        )
        self.integration.save()

        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="ECHO-1001",
            booker_name="Ana Anić",
            booker_phone="+385 91 111 1111",
            check_in=today + timedelta(days=2),
            check_out=today + timedelta(days=5),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            phone="+385911111111",
            is_primary=True,
        )

    def tearDown(self):
        os.environ.pop("WHATSAPP_WEBHOOK_VERIFY_TOKEN", None)
        os.environ.pop("WHATSAPP_APP_SECRET", None)

    def _url(self):
        return "/api/v1/integrations/whatsapp/webhook/"

    def _post_echo(self, payload: dict):
        request = _signed_post(self.factory, self._url(), payload)
        return WhatsAppWebhookView.as_view()(request)

    def test_stores_business_app_echo_as_outbound(self):
        echo_obj = {
            "from": "385911111111",
            "to": self.guest_wa_id,
            "id": "wamid.echo.basic",
            "timestamp": "1710000000",
            "type": "text",
            "text": {"body": "Odgovor iz appa"},
            "meta_custom_future_field": {"nested": True},
        }
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA-ID",
                    "changes": [
                        {
                            "field": "smb_message_echoes",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "385911111111",
                                    "phone_number_id": self.phone_number_id,
                                },
                                "message_echoes": [echo_obj],
                            },
                        }
                    ],
                }
            ],
        }
        with patch(
            "apps.integrations.whatsapp.webhook_service.process_inbound_message.delay"
        ) as mock_inbound:
            response = self._post_echo(payload)

        self.assertEqual(response.status_code, 200)
        row = WhatsAppMessage.objects.get(wamid="wamid.echo.basic")
        self.assertEqual(row.direction, WhatsAppMessage.Direction.OUTBOUND)
        self.assertEqual(row.source, WhatsAppMessage.Source.BUSINESS_APP)
        self.assertEqual(row.wa_id, self.guest_wa_id)
        self.assertEqual(row.body, "Odgovor iz appa")
        self.assertEqual(row.raw_payload, echo_obj)
        self.assertIsNotNone(row.received_at)
        self.assertEqual(row.reservation_id, self.reservation.pk)
        mock_inbound.assert_not_called()

        timeline = serialize_whatsapp(row)
        self.assertEqual(timeline["whatsapp_source"], "business_app")
        self.assertEqual(timeline["source"], "whatsapp")
        self.assertEqual(timeline["direction"], "outbound")

    def test_redelivery_is_idempotent_without_version_bump(self):
        payload = _echo_payload(
            phone_number_id=self.phone_number_id,
            guest_wa_id=self.guest_wa_id,
            wamid="wamid.echo.redeploy",
            body="Prva",
        )
        with patch(
            "apps.integrations.whatsapp.webhook_service.touch_reservation_version"
        ) as mock_touch:
            self.assertEqual(self._post_echo(payload).status_code, 200)
            self.assertEqual(mock_touch.call_count, 1)
            self.assertEqual(self._post_echo(payload).status_code, 200)
            self.assertEqual(mock_touch.call_count, 1)

        self.assertEqual(
            WhatsAppMessage.objects.filter(wamid="wamid.echo.redeploy").count(),
            1,
        )

    def test_cloud_api_wamid_ignores_echo(self):
        WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.echo.cloud",
            wa_id=self.guest_wa_id,
            phone_number_id=self.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            source=WhatsAppMessage.Source.CLOUD_API,
            message_type="text",
            body="Sent via API",
            raw_payload={"messages": [{"id": "wamid.echo.cloud"}]},
        )
        payload = _echo_payload(
            phone_number_id=self.phone_number_id,
            guest_wa_id=self.guest_wa_id,
            wamid="wamid.echo.cloud",
            body="Echo of API send",
        )
        with patch(
            "apps.integrations.whatsapp.webhook_service.touch_reservation_version"
        ) as mock_touch:
            response = self._post_echo(payload)
            mock_touch.assert_not_called()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            WhatsAppMessage.objects.filter(wamid="wamid.echo.cloud").count(),
            1,
        )
        row = WhatsAppMessage.objects.get(wamid="wamid.echo.cloud")
        self.assertEqual(row.source, WhatsAppMessage.Source.CLOUD_API)
        self.assertEqual(row.body, "Sent via API")

    def test_unmatched_echo_stores_without_reservation(self):
        unknown_wa = "385998887776"
        payload = _echo_payload(
            phone_number_id=self.phone_number_id,
            guest_wa_id=unknown_wa,
            wamid="wamid.echo.unmatched",
            body="Prije threada",
        )
        with patch(
            "apps.integrations.whatsapp.webhook_service.touch_reservation_version"
        ) as mock_touch:
            response = self._post_echo(payload)
            mock_touch.assert_not_called()

        self.assertEqual(response.status_code, 200)
        row = WhatsAppMessage.objects.get(wamid="wamid.echo.unmatched")
        self.assertIsNone(row.reservation_id)
        self.assertEqual(row.source, WhatsAppMessage.Source.BUSINESS_APP)
        self.assertEqual(row.wa_id, unknown_wa)

    def test_thread_match_links_reservation(self):
        WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.prior.outbound",
            wa_id=self.guest_wa_id,
            phone_number_id=self.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            source=WhatsAppMessage.Source.CLOUD_API,
            message_type="text",
            body="Prior API outbound",
        )
        # Remove guest phone so phone match alone would fail — thread should win.
        Guest.objects.filter(reservation=self.reservation).update(phone="")
        Reservation.objects.filter(pk=self.reservation.pk).update(booker_phone="")

        payload = _echo_payload(
            phone_number_id=self.phone_number_id,
            guest_wa_id=self.guest_wa_id,
            wamid="wamid.echo.thread",
            body="Via thread",
        )
        response = self._post_echo(payload)
        self.assertEqual(response.status_code, 200)
        row = WhatsAppMessage.objects.get(wamid="wamid.echo.thread")
        self.assertEqual(row.reservation_id, self.reservation.pk)
        self.assertTrue(
            ReservationVersion.objects.filter(
                reservation_id=self.reservation.pk,
                scope=ReservationVersionScope.MESSAGES,
            ).exists()
        )

    def test_unknown_type_does_not_fail(self):
        payload = _echo_payload(
            phone_number_id=self.phone_number_id,
            guest_wa_id=self.guest_wa_id,
            wamid="wamid.echo.sticker",
            message_type="sticker",
        )
        response = self._post_echo(payload)
        self.assertEqual(response.status_code, 200)
        row = WhatsAppMessage.objects.get(wamid="wamid.echo.sticker")
        self.assertEqual(row.message_type, "sticker")
        self.assertEqual(row.source, WhatsAppMessage.Source.BUSINESS_APP)
        self.assertIn("sticker", row.raw_payload)
