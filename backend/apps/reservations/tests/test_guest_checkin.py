from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from apps.properties.models import Property
from apps.reservations.checkin_readiness import (
    all_required_slots_ready,
    build_checkin_readiness,
    effective_session_status,
)
from apps.reservations.guest_checkin_events import (
    GuestSessionReadyEvent,
    emit_guest_session_ready,
)
from apps.reservations.guest_checkin_orchestrator import (
    GuestCheckInOrchestrator,
    GuestCheckInOrchestratorError,
)
from apps.reservations.guest_checkin_session import (
    ensure_active_session,
    evaluate_session_access,
    guest_checkin_window,
    regenerate_session,
)
from apps.reservations.guest_validation import GuestValidator, SlotReadinessStatus
from apps.reservations.models import (
    Guest,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
    ReservationVersion,
    ReservationVersionScope,
)
from apps.tenants.models import Tenant


class GuestCheckInSessionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Check-in Tenant", slug="checkin-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Check-in Property",
            slug="checkin-property",
            guest_checkin_opens_days_before=7,
        )
        self.check_in = date(2026, 7, 15)
        self.check_out = date(2026, 7, 18)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GC-001",
            check_in=self.check_in,
            check_out=self.check_out,
            adults_count=2,
            booker_name="Ana Anić",
            amount=Decimal("100.00"),
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            is_primary=True,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
        )

    def test_guest_checkin_window_uses_property_days_before(self):
        window = guest_checkin_window(self.reservation)
        self.assertEqual(window.opens_at.date(), self.check_in - timedelta(days=7))
        self.assertEqual(window.expires_at.date(), self.check_out + timedelta(days=1))

    def test_ensure_active_session_is_idempotent(self):
        first = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        second = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        self.assertEqual(first.pk, second.pk)

    def test_regenerate_revokes_previous_active_session(self):
        first = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        old, new = regenerate_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.RECEPTION_MANUAL,
        )
        first.refresh_from_db()
        self.assertEqual(old.pk, first.pk)
        self.assertNotEqual(new.pk, first.pk)
        self.assertEqual(first.status, GuestCheckInSessionStatus.REVOKED)
        self.assertEqual(new.status, GuestCheckInSessionStatus.ACTIVE)

    def test_evaluate_session_access_not_open_yet(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        tz = ZoneInfo("Europe/Zagreb")
        before_open = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
        access = evaluate_session_access(
            session,
            self.reservation,
            now=before_open,
        )
        self.assertFalse(access.allowed)
        self.assertEqual(access.http_status, 403)
        self.assertEqual(access.gate_status, "not_open_yet")

    def test_effective_status_ready_is_derived_not_persisted(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        guests = list(self.reservation.guests.order_by("-is_primary", "pk"))
        self._fill_guest(guests[0], suffix="1")
        self._fill_guest(guests[1], suffix="2")

        self.assertTrue(all_required_slots_ready(self.reservation))
        self.assertEqual(effective_session_status(session, self.reservation), "ready")
        session.refresh_from_db()
        self.assertEqual(session.status, GuestCheckInSessionStatus.ACTIVE)

    def test_build_checkin_readiness_counts_ready_slots(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        self._fill_guest(self.reservation.guests.order_by("-is_primary").first(), suffix="1")

        readiness = build_checkin_readiness(session, self.reservation)
        self.assertEqual(readiness.required_slots, 2)
        self.assertEqual(readiness.ready_slots, 1)
        self.assertEqual(readiness.effective_status, GuestCheckInSessionStatus.ACTIVE)
        self.assertFalse(readiness.can_complete)

    def _fill_guest(self, guest: Guest, *, suffix: str) -> None:
        guest.first_name = f"Guest{suffix}"
        guest.last_name = "Test"
        guest.date_of_birth = date(1990, 1, 1)
        guest.nationality = "HR"
        guest.sex = "female"
        guest.document_number = f"DOC{suffix}"
        guest.document_type = "identity_card"
        guest.address = "Grad Zagreb, Ulica 1"
        guest.save()


class GuestValidatorTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Validator Tenant", slug="validator-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Validator Property",
            slug="validator-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GV-001",
            check_in=date(2026, 7, 15),
            check_out=date(2026, 7, 18),
            booker_name="Test Guest",
        )
        self.guest = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=True,
        )

    def test_partial_when_identity_missing(self):
        result = GuestValidator.validate(self.guest, position=1)
        self.assertEqual(result.status, SlotReadinessStatus.PARTIAL)
        self.assertIn("date_of_birth", result.missing_fields)

    def test_ready_when_required_fields_present(self):
        self.guest.first_name = "Marko"
        self.guest.last_name = "Markić"
        self.guest.date_of_birth = date(1985, 5, 5)
        self.guest.nationality = "HR"
        self.guest.sex = "male"
        self.guest.document_number = "123456789"
        self.guest.document_type = "identity_card"
        self.guest.address = "Split, Ulica 2"
        self.guest.save()

        result = GuestValidator.validate(self.guest, position=1)
        self.assertEqual(result.status, SlotReadinessStatus.READY)
        self.assertEqual(result.missing_fields, ())


class GuestCheckInOrchestratorTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Orch Tenant", slug="orch-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Orch Property",
            slug="orch-property",
            guest_checkin_opens_days_before=0,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GO-001",
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=3),
            adults_count=1,
            booker_name="Orch Guest",
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=True,
        )

    def test_patch_slot_emits_session_ready_and_touches_checkin_version(self):
        result = GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        self.assertIn(str(result.session.token), result.url)

        patch = GuestCheckInOrchestrator.patch_slot(
            result.session,
            self.reservation,
            position=1,
            fields={
                "first_name": "Iva",
                "last_name": "Ivić",
                "date_of_birth": "1992-03-04",
                "nationality": "HR",
                "sex": "female",
                "document_number": "99887766",
                "document_type": "identity_card",
                "address": "Zadar, Obala 1",
            },
        )

        self.assertEqual(patch.readiness.effective_status, "ready")
        row = ReservationVersion.objects.get(
            reservation=self.reservation,
            scope=ReservationVersionScope.CHECKIN,
        )
        self.assertEqual(row.version, 1)

    def test_complete_session_requires_ready(self):
        ensured = GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        with self.assertRaises(GuestCheckInOrchestratorError) as ctx:
            GuestCheckInOrchestrator.complete_session(ensured.session, self.reservation)
        self.assertEqual(ctx.exception.code, "not_ready")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_complete_session_marks_completed_and_bumps_version(self):
        ensured = GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        GuestCheckInOrchestrator.patch_slot(
            ensured.session,
            self.reservation,
            position=1,
            fields={
                "first_name": "Iva",
                "last_name": "Ivić",
                "date_of_birth": "1992-03-04",
                "nationality": "HR",
                "sex": "female",
                "document_number": "99887766",
                "document_type": "identity_card",
                "address": "Zadar, Obala 1",
            },
        )

        completed = GuestCheckInOrchestrator.complete_session(
            ensured.session,
            self.reservation,
        )
        self.assertEqual(completed.session.status, GuestCheckInSessionStatus.COMPLETED)
        self.assertIsNotNone(completed.session.completed_at)
        row = ReservationVersion.objects.get(
            reservation=self.reservation,
            scope=ReservationVersionScope.CHECKIN,
        )
        self.assertEqual(row.version, 2)


class GuestCheckInEventsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Events Tenant", slug="events-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Events Property",
            slug="events-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GE-001",
            check_in=date(2026, 7, 15),
            check_out=date(2026, 7, 18),
            booker_name="Events Guest",
        )
        self.session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )

    def test_guest_session_ready_handler_touches_checkin_scope(self):
        emit_guest_session_ready(
            session=self.session,
            reservation=self.reservation,
        )
        row = ReservationVersion.objects.get(
            reservation=self.reservation,
            scope=ReservationVersionScope.CHECKIN,
        )
        self.assertEqual(row.version, 1)

    def test_guest_session_ready_event_is_frozen(self):
        event = GuestSessionReadyEvent(
            session=self.session,
            reservation=self.reservation,
        )
        self.assertEqual(event.session.pk, self.session.pk)
