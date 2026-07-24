"""Phase 6: MESSAGE_ORCHESTRATION_* flags + allowlists + shadow (ADR 0010)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase, override_settings

from apps.communications.messaging.bootstrap import (
    bootstrap_messaging_engine,
    reset_messaging_engine_for_tests,
)
from apps.communications.messaging.flags import (
    orchestration_decision,
    orchestration_runtime,
    resolve_allowlisted_scopes,
    suppress_legacy_automated_outbound,
)
from apps.communications.messaging.health import messaging_health_snapshot
from apps.communications.messaging.models import MessageDispatch, MessageDispatchStatus
from apps.communications.messaging.tasks import run_message_orchestration
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


ZAGREB = ZoneInfo("Europe/Zagreb")


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=False,
    MESSAGE_ORCHESTRATION_SHADOW=True,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)
class OrchestrationFlagsDisabledTests(SimpleTestCase):
    def test_disabled_blocks_even_with_tenant_allowlist(self):
        decision = orchestration_decision(tenant_slug="uzorita")
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.materialize)
        self.assertFalse(decision.live_send)
        self.assertEqual(decision.block_reason, "orchestration_disabled")
        self.assertEqual(decision.mode, "disabled")
        self.assertFalse(suppress_legacy_automated_outbound(tenant_slug="uzorita"))


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=True,
    MESSAGE_ORCHESTRATION_TENANTS=[],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)
class OrchestrationFlagsAllowlistEmptyTests(SimpleTestCase):
    def test_fail_closed_when_both_allowlists_empty(self):
        rt = orchestration_runtime()
        self.assertEqual(rt.block_reason, "allowlist_empty")
        decision = orchestration_decision(tenant_slug="uzorita", runtime=rt)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, "allowlist_empty")


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=True,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita", " Demo "],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)
class OrchestrationTenantAllowlistTests(SimpleTestCase):
    def test_tenant_allowed_shadow_materializes_but_not_live(self):
        decision = orchestration_decision(tenant_slug="UZORITA")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.materialize)
        self.assertTrue(decision.shadow)
        self.assertFalse(decision.live_send)
        self.assertEqual(decision.mode, "shadow")
        self.assertFalse(suppress_legacy_automated_outbound(tenant_slug="uzorita"))

    def test_tenant_not_allowed(self):
        decision = orchestration_decision(tenant_slug="other")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, "tenant_not_allowed")

    def test_normalizes_allowlist_whitespace_and_case(self):
        decision = orchestration_decision(tenant_slug="demo")
        self.assertTrue(decision.allowed)


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=False,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)
class OrchestrationLiveModeTests(SimpleTestCase):
    def test_live_send_and_legacy_suppress_hook(self):
        decision = orchestration_decision(tenant_slug="uzorita")
        self.assertTrue(decision.live_send)
        self.assertEqual(decision.mode, "live")
        self.assertTrue(suppress_legacy_automated_outbound(tenant_slug="uzorita"))


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=False,
    MESSAGE_ORCHESTRATION_TENANTS=[],
    MESSAGE_ORCHESTRATION_PROPERTIES=["villa", "other:beach", "99"],
)
class OrchestrationPropertyAllowlistTests(SimpleTestCase):
    def test_property_slug_match(self):
        decision = orchestration_decision(
            tenant_slug="uzorita",
            property_slug="villa",
            property_id=1,
        )
        self.assertTrue(decision.allowed)

    def test_property_tenant_qualified_slug(self):
        decision = orchestration_decision(
            tenant_slug="other",
            property_slug="beach",
        )
        self.assertTrue(decision.allowed)

    def test_property_id_match(self):
        decision = orchestration_decision(
            tenant_slug="uzorita",
            property_id=99,
        )
        self.assertTrue(decision.allowed)

    def test_property_not_allowed(self):
        decision = orchestration_decision(
            tenant_slug="uzorita",
            property_slug="cabin",
            property_id=1,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, "property_not_allowed")


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=False,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    MESSAGE_ORCHESTRATION_PROPERTIES=["villa"],
)
class OrchestrationPropertyAndTenantGateTests(SimpleTestCase):
    def test_property_match_but_tenant_gate_fails(self):
        decision = orchestration_decision(
            tenant_slug="demo",
            property_slug="villa",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, "tenant_not_allowed")

    def test_property_and_tenant_both_match(self):
        decision = orchestration_decision(
            tenant_slug="uzorita",
            property_slug="villa",
        )
        self.assertTrue(decision.allowed)


@override_settings(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=True,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)
class OrchestrationFlagsHealthTests(SimpleTestCase):
    def test_health_includes_flags(self):
        snap = messaging_health_snapshot(include_queue=False)
        self.assertIn("flags", snap)
        self.assertTrue(snap["flags"]["enabled"])
        self.assertTrue(snap["flags"]["shadow"])
        self.assertEqual(snap["flags"]["mode"], "shadow")
        self.assertEqual(snap["flags"]["tenants"], ["uzorita"])


class OrchestrationCeleryTaskTests(TestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)
        self.tenant = Tenant.objects.create(
            name="Uzorita",
            slug="uzorita",
            default_language="en",
            timezone="Europe/Zagreb",
        )
        self.other = Tenant.objects.create(
            name="Other",
            slug="other",
            default_language="en",
            timezone="Europe/Zagreb",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            timezone="Europe/Zagreb",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(11, 15),
            whatsapp_welcome_send_time=time(11, 15),
        )
        Property.objects.create(
            tenant=self.other,
            name="Other Prop",
            slug="other-prop",
            timezone="Europe/Zagreb",
        )

    def tearDown(self):
        reset_messaging_engine_for_tests()

    def _make_reservation(self, *, check_in: date) -> Reservation:
        return Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Guest",
            booker_email="guest@example.com",
            booker_phone="+38591111111",
            status=Reservation.Status.EXPECTED,
            check_in=check_in,
            check_out=check_in + timedelta(days=2),
        )

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=False,
        MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    )
    def test_task_noop_when_disabled(self):
        result = run_message_orchestration()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["reason"], "orchestration_disabled")
        self.assertEqual(result["materialized"], 0)

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=True,
        MESSAGE_ORCHESTRATION_SHADOW=True,
        MESSAGE_ORCHESTRATION_TENANTS=[],
        MESSAGE_ORCHESTRATION_PROPERTIES=[],
    )
    def test_task_noop_when_allowlist_empty(self):
        result = run_message_orchestration()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["reason"], "allowlist_empty")

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=True,
        MESSAGE_ORCHESTRATION_SHADOW=True,
        MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
        MESSAGE_ORCHESTRATION_PROPERTIES=[],
    )
    def test_resolve_scopes_whole_tenant(self):
        scopes = resolve_allowlisted_scopes()
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].tenant_id, self.tenant.pk)
        self.assertIsNone(scopes[0].property_id)

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=True,
        MESSAGE_ORCHESTRATION_SHADOW=True,
        MESSAGE_ORCHESTRATION_TENANTS=[],
        MESSAGE_ORCHESTRATION_PROPERTIES=["uzorita:uzorita"],
    )
    def test_resolve_scopes_property_token(self):
        scopes = resolve_allowlisted_scopes()
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].property_id, self.property.pk)

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=True,
        MESSAGE_ORCHESTRATION_SHADOW=True,
        MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
        MESSAGE_ORCHESTRATION_PROPERTIES=[],
    )
    @patch("apps.communications.messaging.dispatcher.process_due_dispatches")
    def test_shadow_materializes_without_dispatch(self, mock_process):
        # Platform default: 7d @ 09:00 → check_in 2026-08-08 when now is 2026-08-01.
        self._make_reservation(check_in=date(2026, 8, 8))
        fake_now = datetime(2026, 8, 1, 10, 0, tzinfo=ZAGREB)
        with patch(
            "apps.communications.messaging.scheduler.timezone.now",
            return_value=fake_now,
        ):
            result = run_message_orchestration()

        self.assertEqual(result["mode"], "shadow")
        self.assertGreaterEqual(result["materialized"], 1)
        mock_process.assert_not_called()
        self.assertTrue(
            MessageDispatch.objects.filter(
                reservation__property=self.property,
                status=MessageDispatchStatus.PLANNED,
            ).exists()
        )

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=True,
        MESSAGE_ORCHESTRATION_SHADOW=False,
        MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
        MESSAGE_ORCHESTRATION_PROPERTIES=[],
    )
    @patch(
        "apps.communications.messaging.dispatcher.process_due_dispatches",
        return_value=[],
    )
    def test_live_calls_process_due(self, mock_process):
        result = run_message_orchestration()
        self.assertEqual(result["mode"], "live")
        mock_process.assert_called()
        self.assertTrue(suppress_legacy_automated_outbound(tenant_slug="uzorita"))
