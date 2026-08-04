from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.integrations.channex.booking_service import (
    backfill_channex_financial_fields,
    channex_external_id,
    process_channex_booking_revision,
    process_channex_booking_revisions_feed,
    resolve_ingest_property,
)
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.models import ChannexBookingRevision, IntegrationConfig
from apps.integrations.tests.test_booking_room_mismatch import _assert_single_reception_push
from apps.properties.models import Property, Unit
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.tenants.models import Tenant


class ChannexBookingIngestTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="channex-bcom-test",
            name="Booking test",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="BCOM-STUDIO",
            name="Studio",
            capacity_max_guests=2,
            capacity_adults=2,
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "environment": "staging",
                "base_url": "https://staging.channex.io/api/v1",
                "property_id": "e00e6034-c154-4754-b5d9-9fff73ad12f6",
                "api_key": "test-key",
                "certification_property_slug": "channex-bcom-test",
                "booking_test_rooms": [
                    {
                        "unit_code": "BCOM-STUDIO",
                        "unit_id": self.unit.id,
                        "channex_room_type_id": "18c437d7-13e3-4dbc-9565-48fad4832bf5",
                        "channex_title": "Studio",
                    }
                ],
            }
        )
        self.integration.save()

        self.revision_id = "09a123e3-9011-4077-b0fa-357b01e86bd5"
        self.booking_id = "164f2183-454c-40ea-905d-d79859136236"
        self.revision_payload = {
            "id": self.revision_id,
            "attributes": {
                "booking_id": self.booking_id,
                "status": "new",
                "arrival_date": "2026-05-19",
                "departure_date": "2026-05-23",
                "amount": "316.00",
                "currency": "GBP",
                "ota_name": "Offline",
                "inserted_at": "2026-05-19T05:50:12.114152Z",
                "occupancy": {"adults": 1, "children": 0, "infants": 0},
                "customer": {
                    "name": "Ante",
                    "surname": "Vrcan",
                    "mail": "guest@example.com",
                    "phone": "0976713511",
                    "address": "Test street 1",
                    "city": "Šibenik",
                    "country": "HR",
                },
                "rooms": [
                    {
                        "amount": "316.00",
                        "room_type_id": "18c437d7-13e3-4dbc-9565-48fad4832bf5",
                        "rate_plan_id": "6734ae1e-70bb-4217-b668-2aa8720bca13",
                    }
                ],
            },
        }

    def test_channex_external_id_prefix(self):
        self.assertEqual(
            channex_external_id("abc"),
            "channex:abc",
        )

    def test_booking_test_room_lookup(self):
        config = ChannexRuntimeConfig.from_integration_dict(self.integration.get_config_dict())
        link = config.booking_test_room_for_channex_room_type_id(
            "18c437d7-13e3-4dbc-9565-48fad4832bf5"
        )
        self.assertIsNotNone(link)
        self.assertEqual(link.unit_code, "BCOM-STUDIO")

    def test_room_types_lookup_for_production_mapping(self):
        production_property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        production_unit = Unit.objects.create(
            tenant=self.tenant,
            property=production_property,
            code="R1",
            name="R1",
            capacity_max_guests=2,
            capacity_adults=2,
        )
        self.integration.set_config_dict(
            {
                **self.integration.get_config_dict(),
                "sync_property_slug": "uzorita",
                "certification_property_slug": "channex-bcom-test",
                "room_types": [
                    {
                        "unit_code": "R1",
                        "unit_id": production_unit.id,
                        "channex_room_type_id": "prod-room-type-uuid",
                        "channex_title": "Luxury Room Uzorita - R1",
                    }
                ],
            }
        )
        self.integration.save()
        config = ChannexRuntimeConfig.from_integration_dict(self.integration.get_config_dict())
        link = config.room_link_for_channex_room_type_id("prod-room-type-uuid")
        self.assertIsNotNone(link)
        self.assertEqual(link.unit_code, "R1")
        ingest_property = resolve_ingest_property(self.tenant, config)
        self.assertEqual(ingest_property.slug, "uzorita")

    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_ingest_creates_reservation_and_acknowledges(self, mock_client_cls, mock_push_inventory):
        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            self.revision_id,
        )

        self.assertEqual(reservation.external_id, channex_external_id(self.booking_id))
        self.assertEqual(reservation.check_in, date(2026, 5, 19))
        self.assertEqual(reservation.check_out, date(2026, 5, 23))
        self.assertEqual(reservation.status, Reservation.Status.EXPECTED)
        self.assertEqual(reservation.booker_name, "Ante Vrcan")
        self.assertEqual(reservation.booker_country, "HR")
        self.assertEqual(reservation.amount, Decimal("316.00"))
        self.assertEqual(reservation.currency, "GBP")
        self.assertEqual(reservation.source, "Offline")
        self.assertEqual(reservation.adults_count, 1)
        self.assertEqual(reservation.children_count, 0)
        self.assertEqual(reservation.infants_count, 0)
        self.assertEqual(reservation.persons_count, 1)

        units = list(ReservationUnit.objects.filter(reservation=reservation))
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].unit_id, self.unit.id)
        self.assertEqual(units[0].room_name, "Studio")

        guest = Guest.objects.get(reservation=reservation, is_primary=True)
        self.assertEqual(guest.first_name, "Ante")
        self.assertEqual(guest.last_name, "Vrcan")
        self.assertEqual(guest.nationality, "HR")
        self.assertEqual(guest.document_country_iso2, "HR")

        mock_client.acknowledge_booking_revision.assert_called_once_with(self.revision_id)
        self.assertTrue(
            ChannexBookingRevision.objects.filter(revision_id=self.revision_id).exists()
        )

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_ingest_is_idempotent_per_revision(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        first = process_channex_booking_revision(self.integration, self.revision_id)
        second = process_channex_booking_revision(self.integration, self.revision_id)

        self.assertEqual(first.id, second.id)
        mock_client.get_booking_revision.assert_called_once()
        mock_client.acknowledge_booking_revision.assert_called_once()

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_revision_preserves_usable_email_over_ota_relay(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            self.revision_id,
        )
        reservation.booker_email = "reissitua@gmail.com"
        reservation.save(update_fields=["booker_email", "updated_at"])
        guest = Guest.objects.get(reservation=reservation, is_primary=True)
        guest.email = "reissitua@gmail.com"
        guest.save(update_fields=["email", "updated_at"])

        relay_payload = dict(self.revision_payload)
        relay_payload["id"] = "relay-overwrite-revision"
        relay_payload["attributes"] = {
            **self.revision_payload["attributes"],
            "status": "modified",
            "customer": {
                **self.revision_payload["attributes"]["customer"],
                "mail": "bfouqu.690243@guest.booking.com",
            },
        }
        mock_client.get_booking_revision.return_value = relay_payload

        updated = process_channex_booking_revision(
            self.integration,
            "relay-overwrite-revision",
        )
        updated.refresh_from_db()
        guest.refresh_from_db()
        self.assertEqual(updated.pk, reservation.pk)
        self.assertEqual(updated.booker_email, "reissitua@gmail.com")
        self.assertEqual(guest.email, "reissitua@gmail.com")

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_cancelled_revision_updates_status(self, mock_client_cls):
        cancelled = dict(self.revision_payload)
        cancelled["attributes"] = dict(self.revision_payload["attributes"])
        cancelled["attributes"]["status"] = "cancelled"

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = cancelled
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            self.revision_id,
        )
        self.assertEqual(reservation.status, Reservation.Status.CANCELED)
        self.assertIsNotNone(reservation.canceled_at)

    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_cancelled_revision_without_dates_preserves_units(
        self, mock_client_cls, mock_push_inventory
    ):
        """B.com cancel payloads often drop arrival/departure and rooms."""
        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            "active-revision-id",
        )
        unit = self.unit
        ReservationUnit.objects.filter(reservation=reservation).update(unit=unit)

        cancelled = dict(self.revision_payload)
        cancelled["id"] = "cancel-revision-id"
        cancelled["attributes"] = {
            **self.revision_payload["attributes"],
            "status": "cancelled",
            "arrival_date": None,
            "departure_date": None,
            "rooms": [],
            "occupancy": {"adults": 0, "children": 0, "infants": 0},
            "amount": "0.00",
        }
        mock_client.get_booking_revision.return_value = cancelled

        with self.captureOnCommitCallbacks(execute=True):
            updated = process_channex_booking_revision(
                self.integration,
                "cancel-revision-id",
            )

        self.assertEqual(updated.pk, reservation.pk)
        self.assertEqual(updated.status, Reservation.Status.CANCELED)
        self.assertEqual(updated.check_in, reservation.check_in)
        self.assertEqual(updated.check_out, reservation.check_out)
        self.assertEqual(
            ReservationUnit.objects.filter(reservation=updated, unit=unit).count(),
            1,
        )
        mock_push_inventory.assert_called()

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_ingest_stores_infants_separately_from_persons_count(self, mock_client_cls):
        payload = dict(self.revision_payload)
        payload["attributes"] = dict(self.revision_payload["attributes"])
        payload["attributes"]["occupancy"] = {"adults": 2, "children": 1, "infants": 2}

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            "infants-revision-id",
        )

        self.assertEqual(reservation.adults_count, 2)
        self.assertEqual(reservation.children_count, 1)
        self.assertEqual(reservation.infants_count, 2)
        self.assertEqual(reservation.persons_count, 3)

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_feed_processes_only_unseen_revisions(self, mock_client_cls):
        other_revision_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        other_booking_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        other_payload = {
            "id": other_revision_id,
            "attributes": {
                **self.revision_payload["attributes"],
                "booking_id": other_booking_id,
            },
        }

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        process_channex_booking_revision(
            self.integration,
            self.revision_id,
            client=mock_client,
        )

        mock_client.reset_mock()
        mock_client.list_booking_revisions_feed.return_value = [
            self.revision_id,
            other_revision_id,
        ]

        def get_revision(revision_id: str):
            if revision_id == self.revision_id:
                return self.revision_payload
            return other_payload

        mock_client.get_booking_revision.side_effect = get_revision

        processed = process_channex_booking_revisions_feed(
            self.integration,
            client=mock_client,
        )

        self.assertEqual(len(processed["ingested"]), 1)
        self.assertEqual(processed["ack_only"], 0)
        self.assertEqual(processed["ingested"][0].external_id, channex_external_id(other_booking_id))
        mock_client.get_booking_revision.assert_called_once_with(other_revision_id)
        mock_client.acknowledge_booking_revision.assert_called_once_with(other_revision_id)

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_orphan_cancelled_revision_ack_only_without_reservation(self, mock_client_cls):
        orphan_revision_id = "533c748d-aaaa-bbbb-cccc-533c748d0001"
        orphan_booking_id = "11111111-1111-1111-1111-111111111111"
        orphan_payload = {
            "id": orphan_revision_id,
            "attributes": {
                "booking_id": orphan_booking_id,
                "status": "cancelled",
                "ota_reservation_code": "6262102168",
                "arrival_date": None,
                "departure_date": None,
            },
        }

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = orphan_payload
        mock_client_cls.return_value = mock_client

        result = process_channex_booking_revision(
            self.integration,
            orphan_revision_id,
        )

        self.assertIsNone(result)
        self.assertFalse(
            Reservation.objects.filter(
                external_id=channex_external_id(orphan_booking_id)
            ).exists()
        )
        revision_row = ChannexBookingRevision.objects.get(revision_id=orphan_revision_id)
        self.assertIsNone(revision_row.reservation_id)
        self.assertEqual(revision_row.booking_id, orphan_booking_id)
        self.assertEqual(revision_row.channex_status, "cancelled")
        mock_client.acknowledge_booking_revision.assert_called_once_with(orphan_revision_id)

    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_feed_continues_after_orphan_cancel(self, mock_client_cls, mock_push_inventory):
        orphan_revision_id = "5146f513-aaaa-bbbb-cccc-5146f5130001"
        orphan_booking_id = "22222222-2222-2222-2222-222222222222"
        normal_revision_id = "fb3ddd6b-bbbb-cccc-dddd-fb3ddd6b0001"
        normal_booking_id = "33333333-3333-3333-3333-333333333333"

        orphan_payload = {
            "id": orphan_revision_id,
            "attributes": {
                "booking_id": orphan_booking_id,
                "status": "cancelled",
                "ota_reservation_code": "5034902027",
                "arrival_date": None,
                "departure_date": None,
            },
        }
        normal_payload = {
            "id": normal_revision_id,
            "attributes": {
                **self.revision_payload["attributes"],
                "booking_id": normal_booking_id,
                "status": "modified",
                "ota_reservation_code": "6860894044",
            },
        }

        mock_client = MagicMock()
        mock_client.list_booking_revisions_feed.return_value = [
            orphan_revision_id,
            normal_revision_id,
        ]

        def get_revision(revision_id: str):
            if revision_id == orphan_revision_id:
                return orphan_payload
            return normal_payload

        mock_client.get_booking_revision.side_effect = get_revision
        mock_client_cls.return_value = mock_client

        result = process_channex_booking_revisions_feed(
            self.integration,
            client=mock_client,
        )

        self.assertEqual(result["ack_only"], 1)
        self.assertEqual(len(result["ingested"]), 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(
            result["ingested"][0].external_id,
            channex_external_id(normal_booking_id),
        )
        self.assertEqual(mock_client.acknowledge_booking_revision.call_count, 2)
        self.assertTrue(
            ChannexBookingRevision.objects.filter(
                revision_id=orphan_revision_id,
                reservation__isnull=True,
            ).exists()
        )

    @patch("apps.core.notifications.send_tenant_reception_push")
    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_ingest_flags_overbooking_when_unit_already_occupied(
        self,
        mock_client_cls,
        mock_push_inventory,
        mock_reception_push,
    ):
        incumbent = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="6931685558",
            booking_code="6931685558",
            booker_name="Sladjana SKORIC",
            check_in=date(2026, 5, 19),
            check_out=date(2026, 5, 23),
            status=Reservation.Status.EXPECTED,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=incumbent,
            unit=self.unit,
            room_name="Studio",
        )

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            "overbooking-revision-id",
        )

        reservation.refresh_from_db()
        self.assertIn("OVERBOOKING:", reservation.notes)
        self.assertIn("6931685558", reservation.notes)
        _assert_single_reception_push(
            self,
            mock_reception_push,
            event_type="reservation.overbooking",
            reservation_id=reservation.pk,
        )

    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_ingest_maps_ota_commission_and_payment_fields(self, mock_client_cls, mock_push_inventory):
        payload = dict(self.revision_payload)
        payload["attributes"] = {
            **self.revision_payload["attributes"],
            "ota_name": "Booking.com",
            "ota_commission": "37.35",
            "payment_collect": "ota",
            "payment_type": "bank_transfer",
        }

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            "commission-revision-id",
        )

        self.assertEqual(reservation.commission_amount, Decimal("37.35"))
        self.assertEqual(reservation.commission_percent, Decimal("11.82"))
        self.assertEqual(reservation.payment_provider, "Payments by Booking.com")
        self.assertEqual(
            reservation.payment_status,
            "Payment is facilitated through Payments by Booking.com",
        )

    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_ingest_without_ota_commission_preserves_existing_commission(
        self, mock_client_cls, mock_push_inventory
    ):
        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            "preserve-commission-revision-id",
        )
        Reservation.objects.filter(pk=reservation.pk).update(
            commission_amount=Decimal("50.00"),
            commission_percent=Decimal("15.82"),
            payment_provider="From XLS",
            payment_status="Paid via XLS",
        )

        modified_payload = dict(self.revision_payload)
        modified_payload["attributes"] = {
            **self.revision_payload["attributes"],
            "status": "modified",
        }
        mock_client.get_booking_revision.return_value = modified_payload

        updated = process_channex_booking_revision(
            self.integration,
            "preserve-commission-revision-id-2",
        )

        self.assertEqual(updated.pk, reservation.pk)
        self.assertEqual(updated.commission_amount, Decimal("50.00"))
        self.assertEqual(updated.commission_percent, Decimal("15.82"))
        self.assertEqual(updated.payment_provider, "From XLS")
        self.assertEqual(updated.payment_status, "Paid via XLS")

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_backfill_financial_fields_from_booking_api(self, mock_client_cls):
        booking_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(booking_id),
            booking_code="BDC-123",
            booker_name="Jane Doe",
            check_in=date(2026, 6, 1),
            check_out=date(2026, 6, 3),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
            amount=Decimal("315.90"),
        )

        mock_client = MagicMock()
        mock_client.get_booking.return_value = {
            "id": booking_id,
            "attributes": {
                "amount": "315.90",
                "ota_commission": "37.35",
                "ota_name": "Booking.com",
                "payment_collect": "ota",
            },
        }
        mock_client_cls.return_value = mock_client

        stats = backfill_channex_financial_fields(self.integration, client=mock_client)

        reservation.refresh_from_db()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(reservation.commission_amount, Decimal("37.35"))
        self.assertEqual(reservation.commission_percent, Decimal("11.82"))
        self.assertEqual(reservation.payment_provider, "Payments by Booking.com")
        mock_client.get_booking.assert_called_once_with(booking_id)

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_backfill_skips_reservations_with_existing_commission(self, mock_client_cls):
        booking_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(booking_id),
            booking_code="BDC-456",
            booker_name="John Doe",
            check_in=date(2026, 6, 1),
            check_out=date(2026, 6, 3),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
            commission_amount=Decimal("50.00"),
        )

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        stats = backfill_channex_financial_fields(self.integration, client=mock_client)

        self.assertEqual(stats["processed"], 0)
        mock_client.get_booking.assert_not_called()

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_backfill_dry_run_does_not_save(self, mock_client_cls):
        booking_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(booking_id),
            booking_code="BDC-789",
            booker_name="Dry Run",
            check_in=date(2026, 6, 1),
            check_out=date(2026, 6, 3),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
        )

        mock_client = MagicMock()
        mock_client.get_booking.return_value = {
            "id": booking_id,
            "attributes": {
                "amount": "100.00",
                "ota_commission": "10.00",
                "ota_name": "Booking.com",
                "payment_collect": "ota",
            },
        }
        mock_client_cls.return_value = mock_client

        stats = backfill_channex_financial_fields(
            self.integration,
            dry_run=True,
            client=mock_client,
        )

        reservation.refresh_from_db()
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(len(stats["updates"]), 1)
        self.assertIsNone(reservation.commission_amount)

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_backfill_resolves_legacy_reservation_by_booking_code(self, mock_client_cls):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="140327897",
            booking_code="6366775609",
            booker_name="Ante Vrcan",
            check_in=date(2026, 12, 14),
            check_out=date(2026, 12, 15),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
            amount=Decimal("62.30"),
        )

        mock_client = MagicMock()
        mock_client.get_booking.side_effect = AssertionError("should not call get_booking")
        mock_client.find_booking_by_ota_reservation_code.return_value = {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "attributes": {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "amount": "62.30",
                "ota_commission": "7.40",
                "ota_name": "Booking.com",
                "payment_collect": "ota",
            },
        }
        mock_client_cls.return_value = mock_client

        stats = backfill_channex_financial_fields(self.integration, client=mock_client)

        reservation.refresh_from_db()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(reservation.commission_amount, Decimal("7.40"))
        mock_client.find_booking_by_ota_reservation_code.assert_called_once_with("6366775609")
        self.assertEqual(stats["updates"][0]["lookup_method"], "booking_code")

    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_backfill_skips_legacy_blocked_channel_without_booking_code(self, mock_client_cls):
        Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="140574507",
            booking_code="",
            booker_name="Block R6",
            check_in=date(2026, 11, 2),
            check_out=date(2026, 11, 8),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
        )

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        stats = backfill_channex_financial_fields(self.integration, client=mock_client)

        self.assertEqual(stats["skipped_no_lookup_code"], 1)
        mock_client.find_booking_by_ota_reservation_code.assert_not_called()
        mock_client.get_booking.assert_not_called()

    @patch(
        "apps.integrations.channex.reservation_availability_service.push_channex_inventory_after_ingest"
    )
    @patch("apps.integrations.channex.booking_service.ChannexClient")
    def test_second_revision_preserves_units_when_channex_under_reports(
        self, mock_client_cls, mock_push_inventory
    ):
        """Multi-room corrected in stay.hr must not be wiped by a thin Channex revision."""
        unit_r2 = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="BCOM-R2",
            name="R2",
            capacity_max_guests=2,
            capacity_adults=2,
        )
        second_revision_id = "22222222-2222-2222-2222-222222222222"

        mock_client = MagicMock()
        mock_client.get_booking_revision.return_value = self.revision_payload
        mock_client_cls.return_value = mock_client

        reservation = process_channex_booking_revision(
            self.integration,
            self.revision_id,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            unit=unit_r2,
            sort_order=1,
            room_name="R2",
        )
        reservation.units_count = 2
        reservation.save(update_fields=["units_count", "updated_at"])

        mock_client.reset_mock()
        mock_client.get_booking_revision.return_value = self.revision_payload

        process_channex_booking_revision(
            self.integration,
            second_revision_id,
        )

        codes = sorted(
            ReservationUnit.objects.filter(reservation=reservation, unit_id__isnull=False)
            .values_list("unit__code", flat=True)
        )
        self.assertEqual(codes, ["BCOM-R2", "BCOM-STUDIO"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.units_count, 2)
