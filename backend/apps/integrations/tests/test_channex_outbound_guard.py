"""OutboundGuard fail-closed + audit (ADR 0014 / incident 2026-08-01)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexWriteDisabled
from apps.integrations.channex.outbound_guard import (
    assert_can_write,
    can_write_to_channex,
    force_channex_write,
    get_channex_outbound_allowed_total,
    get_channex_outbound_blocked_total,
    reset_channex_outbound_counters,
    skip_if_channex_write_disabled,
)


class OutboundGuardTests(TestCase):
    def setUp(self):
        reset_channex_outbound_counters()

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    def test_default_fail_closed_blocks(self):
        self.assertFalse(can_write_to_channex())
        with self.assertRaises(ChannexWriteDisabled):
            assert_can_write(operation="availability.repair", caller="cli")
        self.assertEqual(get_channex_outbound_blocked_total(), 1)

    @override_settings(CHANNEX_OUTBOUND_ENABLED=True)
    def test_enabled_allows_and_counts(self):
        assert_can_write(operation="availability.repair", caller="cli", tenant="uzorita")
        self.assertEqual(get_channex_outbound_allowed_total(), 1)
        self.assertEqual(get_channex_outbound_blocked_total(), 0)

    @override_settings(
        CHANNEX_OUTBOUND_ENABLED=True,
        CHANNEX_OUTBOUND_TENANT_SLUGS=["uzorita"],
    )
    def test_allowlist_blocks_other_tenant(self):
        with self.assertRaises(ChannexWriteDisabled) as ctx:
            assert_can_write(tenant="demo", operation="ari", caller="cli")
        self.assertEqual(ctx.exception.reason, "allowlist")

    @override_settings(
        CHANNEX_OUTBOUND_ENABLED=True,
        CHANNEX_OUTBOUND_TENANT_SLUGS=["uzorita"],
    )
    def test_allowlist_allows_listed_tenant(self):
        assert_can_write(tenant="uzorita", operation="ari", caller="cli")

    @override_settings(
        CHANNEX_OUTBOUND_ENABLED=True,
        CHANNEX_OUTBOUND_MAINTENANCE=True,
    )
    def test_maintenance_blocks(self):
        with self.assertRaises(ChannexWriteDisabled) as ctx:
            assert_can_write(operation="ari", caller="cli")
        self.assertEqual(ctx.exception.reason, "maintenance")

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    def test_force_allows_with_warning(self):
        with force_channex_write():
            with self.assertLogs(
                "apps.integrations.channex.outbound_guard", level="WARNING"
            ) as logs:
                assert_can_write(operation="POST /availability", caller="cli")
        self.assertTrue(
            any("CHANNEX FORCE WRITE" in line for line in logs.output),
            logs.output,
        )
        self.assertEqual(get_channex_outbound_allowed_total(), 1)

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    def test_client_non_get_blocked(self):
        cfg = ChannexRuntimeConfig(
            environment="staging",
            api_key="k",
            property_id="p",
            base_url="https://example.test/api/v1",
            room_types=(),
        )
        client = ChannexClient(cfg)
        client._session = MagicMock()
        with self.assertRaises(ChannexWriteDisabled):
            client._request("POST", "/availability", json={})
        client._session.request.assert_not_called()

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    def test_skip_helper_for_write_tasks(self):
        skipped = skip_if_channex_write_disabled(task="flush", tenant="uzorita")
        self.assertIsNotNone(skipped)
        self.assertTrue(skipped["skipped"])
        self.assertEqual(skipped["reason"], "disabled")
