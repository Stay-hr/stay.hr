from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from apps.integrations.evisitor.config import EvisitorRuntimeConfig
from apps.integrations.evisitor.exceptions import EvisitorApiError
from apps.integrations.evisitor.mapper import build_check_out_payload
from apps.integrations.evisitor.metrics import (
    get_evisitor_checkout_failed_total,
    reset_evisitor_checkout_failed_total,
)
from apps.integrations.evisitor.service import (
    checkout_reservation_guests_in_evisitor,
    submit_guest_checkin,
    submit_guest_checkout,
)
from apps.integrations.evisitor.summary import (
    evisitor_progress_for_guests,
    evisitor_summary_for_guests,
)
from apps.properties.models import Property
from apps.reservations.checkout import CheckoutBlockedError, perform_reservation_checkout
from apps.reservations.models import EvisitorGuestStatus, EvisitorSubmission, Guest, Reservation
from apps.tenants.models import Tenant


def _runtime_config() -> EvisitorRuntimeConfig:
    return EvisitorRuntimeConfig(
        enabled=True,
        env="test",
        base_url="https://test.evisitor.hr/test/rest",
        username="user",
        password="pass",
        api_key="key",
        facility_code="12345",
        default_stay_time_from="15:00",
        default_stay_time_until="10:00",
        default_arrival_organisation="01",
        default_offered_service_type="01",
        default_payment_category="01",
    )


class EvisitorCheckoutFailedTests(TestCase):
    def setUp(self):
        reset_evisitor_checkout_failed_total()
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita-co-fail")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita-co-fail",
        )
        self.check_in = date(2026, 7, 1)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="BK-CO-FAIL",
            check_in=self.check_in,
            check_out=date(2026, 7, 5),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Bernard",
            amount=Decimal("400.00"),
            adults_count=2,
        )
        self.config = _runtime_config()
        self.reg_a = uuid4()
        self.reg_b = uuid4()

    def tearDown(self):
        reset_evisitor_checkout_failed_total()

    def _adult(self, **kwargs) -> Guest:
        defaults = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "first_name": "Julie",
            "last_name": "Bernard",
            "name": "Julie Bernard",
            "date_of_birth": date(1985, 4, 1),
            "sex": "F",
            "nationality": "FR",
            "document_type": "passport",
            "document_number": "P123",
            "document_country_iso2": "FR",
            "document_country_iso3": "FRA",
            "address": "Paris",
            "evisitor_status": EvisitorGuestStatus.SENT,
            "evisitor_registration_id": self.reg_a,
            "is_primary": True,
        }
        defaults.update(kwargs)
        return Guest.objects.create(**defaults)

    def _sent_checkin_submission(self, guest: Guest) -> EvisitorSubmission:
        return EvisitorSubmission.objects.create(
            tenant=self.tenant,
            guest=guest,
            registration_id=guest.evisitor_registration_id,
            status=EvisitorGuestStatus.SENT,
            request_payload={"Tourist": {"ID": str(guest.evisitor_registration_id)}},
            response_payload={"ok": True},
            created_at=timezone.now(),
            submitted_at=timezone.now(),
        )

    @patch("apps.integrations.evisitor.service.resolve_evisitor_config")
    @patch("apps.integrations.evisitor.service.EvisitorClient")
    def test_checkout_failure_sets_checkout_failed_preserves_registration(
        self, mock_client_cls, mock_resolve
    ):
        mock_resolve.return_value = self.config
        client = MagicMock()
        mock_client_cls.return_value = client
        client.execute_action.side_effect = EvisitorApiError(
            "fail",
            user_message="[[[Ne možete izmjeniti podatke prijave nakon dozvoljenog roka izmjena.]]]",
            status_code=400,
        )

        guest = self._adult()
        checkin = self._sent_checkin_submission(guest)

        with self.assertRaises(EvisitorApiError):
            submit_guest_checkout(guest)

        guest.refresh_from_db()
        self.assertEqual(guest.evisitor_status, EvisitorGuestStatus.CHECKOUT_FAILED)
        self.assertEqual(guest.evisitor_registration_id, self.reg_a)
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, EvisitorGuestStatus.SENT)

        attempt = (
            EvisitorSubmission.objects.filter(guest=guest)
            .exclude(pk=checkin.pk)
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, EvisitorGuestStatus.FAILED)
        self.assertIn("CheckOutDate", attempt.request_payload)
        self.assertEqual(get_evisitor_checkout_failed_total(), 1)

    @patch("apps.integrations.evisitor.service.resolve_evisitor_config")
    @patch("apps.integrations.evisitor.service.EvisitorClient")
    def test_already_checked_out_message_is_idempotent_success(
        self, mock_client_cls, mock_resolve
    ):
        """eVisitor auto-checkout → CheckOutTourist OR-message → local checked_out."""
        mock_resolve.return_value = self.config
        client = MagicMock()
        mock_client_cls.return_value = client
        client.execute_action.side_effect = EvisitorApiError(
            "fail",
            user_message=(
                "[[[Ne postoji prijava sa zadanim ID-jem ili je već odjavljena "
                "ili poništena.]]] (ID: a01c2e9f-3839-4f0e-b39b-775e107d6f36)"
            ),
            status_code=400,
        )

        guest = self._adult()
        self._sent_checkin_submission(guest)

        submission = submit_guest_checkout(guest)

        guest.refresh_from_db()
        self.assertEqual(guest.evisitor_status, EvisitorGuestStatus.CHECKED_OUT)
        self.assertEqual(submission.status, EvisitorGuestStatus.CHECKED_OUT)
        self.assertTrue(submission.response_payload.get("already_checked_out"))
        self.assertEqual(get_evisitor_checkout_failed_total(), 0)
        client.execute_action.assert_called_once()
        self.assertEqual(client.execute_action.call_args.args[0], "CheckOutTourist")

    @patch("apps.integrations.evisitor.service.resolve_evisitor_config")
    @patch("apps.integrations.evisitor.service.EvisitorClient")
    def test_retry_checkout_fail_stays_checkout_failed_never_failed(
        self, mock_client_cls, mock_resolve
    ):
        mock_resolve.return_value = self.config
        client = MagicMock()
        mock_client_cls.return_value = client
        client.execute_action.side_effect = EvisitorApiError(
            "fail", user_message="again", status_code=400
        )

        guest = self._adult(evisitor_status=EvisitorGuestStatus.CHECKOUT_FAILED)
        self._sent_checkin_submission(guest)

        with self.assertRaises(EvisitorApiError):
            submit_guest_checkout(guest)

        guest.refresh_from_db()
        self.assertEqual(guest.evisitor_status, EvisitorGuestStatus.CHECKOUT_FAILED)
        self.assertNotEqual(guest.evisitor_status, EvisitorGuestStatus.FAILED)

    @patch("apps.integrations.evisitor.service.resolve_evisitor_config")
    @patch("apps.integrations.evisitor.service.EvisitorClient")
    def test_batch_continues_after_first_failure(self, mock_client_cls, mock_resolve):
        mock_resolve.return_value = self.config
        client = MagicMock()
        mock_client_cls.return_value = client

        guest_a = self._adult(
            first_name="Julie",
            last_name="Bernard",
            name="Julie Bernard",
            evisitor_registration_id=self.reg_a,
        )
        guest_b = self._adult(
            first_name="Julien",
            last_name="Bernard",
            name="Julien Bernard",
            is_primary=False,
            evisitor_registration_id=self.reg_b,
        )
        self._sent_checkin_submission(guest_a)
        self._sent_checkin_submission(guest_b)

        def _execute(action, payload):
            if str(payload.get("ID")) == str(self.reg_a):
                raise EvisitorApiError("fail A", user_message="Julie fail", status_code=400)
            return {"ok": True}

        client.execute_action.side_effect = _execute

        with self.assertRaises(EvisitorApiError) as ctx:
            checkout_reservation_guests_in_evisitor(self.reservation)

        guest_a.refresh_from_db()
        guest_b.refresh_from_db()
        self.assertEqual(guest_a.evisitor_status, EvisitorGuestStatus.CHECKOUT_FAILED)
        self.assertEqual(guest_b.evisitor_status, EvisitorGuestStatus.CHECKED_OUT)
        self.assertEqual(len(ctx.exception.failed_guests), 1)
        self.assertEqual(ctx.exception.failed_guests[0]["guest_id"], guest_a.pk)

    def test_summary_complete_and_progress_sent_for_checkout_failed(self):
        guest = self._adult(evisitor_status=EvisitorGuestStatus.CHECKOUT_FAILED)
        summary = evisitor_summary_for_guests([guest], reference_date=self.check_in)
        self.assertEqual(summary, "complete")
        progress = evisitor_progress_for_guests([guest], reference_date=self.check_in)
        self.assertEqual(progress["sent"], 1)
        self.assertEqual(progress["failed"], 0)

    @patch("apps.integrations.evisitor.mapper.iso2_to_iso3", return_value="FRA")
    def test_mapper_allows_checkout_failed_retry(self, _mock_iso):
        guest = self._adult(evisitor_status=EvisitorGuestStatus.CHECKOUT_FAILED)
        payload = build_check_out_payload(guest, config=self.config)
        self.assertIn("CheckOutDate", payload)

    @patch("apps.integrations.evisitor.service.resolve_evisitor_config")
    def test_checkin_skips_checkout_failed_without_api(self, mock_resolve):
        mock_resolve.return_value = self.config
        guest = self._adult(evisitor_status=EvisitorGuestStatus.CHECKOUT_FAILED)
        checkin = self._sent_checkin_submission(guest)

        with patch("apps.integrations.evisitor.service.EvisitorClient") as mock_client_cls:
            result = submit_guest_checkin(guest, force_retry=True)
            mock_client_cls.assert_not_called()

        self.assertEqual(result.pk, checkin.pk)
        guest.refresh_from_db()
        self.assertEqual(guest.evisitor_status, EvisitorGuestStatus.CHECKOUT_FAILED)

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_perform_checkout_maps_batch_error_to_blocked(self, mock_batch):
        mock_batch.side_effect = EvisitorApiError(
            "Odjava nije uspjela za 1 gosta.",
            user_message="Odjava nije uspjela za 1 gosta.",
            failed_guests=[{"guest_id": 1, "name": "Julie", "reason": "x"}],
        )
        self._adult()
        with self.assertRaises(CheckoutBlockedError) as ctx:
            perform_reservation_checkout(self.reservation)
        self.assertEqual(ctx.exception.code, "evisitor_checkout_failed")
        self.assertEqual(len(ctx.exception.failed_guests), 1)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_IN)
