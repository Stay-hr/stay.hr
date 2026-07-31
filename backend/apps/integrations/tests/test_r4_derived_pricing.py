from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.integrations.models import (
    ChannelRatePlan,
    ChannexAriOutbox,
    IntegrationConfig,
    RatePlanDay,
    SalesChannel,
)
from apps.integrations.pricing.obp import (
    channex_push_rate_for_unit,
    compute_list_rate,
    compute_normal_rate,
    get_obp_policy,
)
from apps.integrations.pricing.r4_derived import (
    compress_rate_days,
    derive_r4_base_rate,
    merge_king_band_day_rates,
    sync_r4_rates_from_king_band,
)
from apps.properties.models import Property, Unit
from apps.tenants.models import Tenant


class R4DerivedPricingUnitTests(TestCase):
    def test_derive_r4_base_rate_summer_band(self):
        self.assertEqual(derive_r4_base_rate(Decimal("113.00")), Decimal("101.70"))

    def test_r4_obp_matches_r1_policy(self):
        base = Decimal("101.70")
        policy = get_obp_policy("R4")
        self.assertEqual(policy.primary_occupancy_adults, 2)
        self.assertEqual(policy.max_adults, 2)
        self.assertEqual(policy.adult_delta, Decimal("5.00"))
        self.assertEqual(compute_normal_rate(base, "R4"), Decimal("106.70"))
        self.assertEqual(channex_push_rate_for_unit("R4", base), Decimal("106.70"))
        self.assertEqual(compute_list_rate(base, 1, unit_code="R4"), Decimal("101.70"))
        self.assertEqual(compute_list_rate(base, 2, unit_code="R4"), Decimal("106.70"))
        self.assertEqual(
            compute_list_rate(base, 2, 1, unit_code="R4"),
            Decimal("108.70"),
        )

    def test_merge_prefers_r1_over_r2(self):
        merged = merge_king_band_day_rates(
            {
                "R1": {date(2026, 7, 1): Decimal("113.00")},
                "R2": {
                    date(2026, 7, 1): Decimal("999.00"),
                    date(2026, 7, 2): Decimal("113.00"),
                },
            }
        )
        self.assertEqual(merged[date(2026, 7, 1)], Decimal("113.00"))
        self.assertEqual(merged[date(2026, 7, 2)], Decimal("113.00"))

    def test_compress_rate_days(self):
        ranges = compress_rate_days(
            {
                date(2026, 7, 1): Decimal("101.70"),
                date(2026, 7, 2): Decimal("101.70"),
                date(2026, 7, 4): Decimal("101.70"),
                date(2026, 7, 5): Decimal("90.00"),
            }
        )
        self.assertEqual(
            ranges,
            [
                (date(2026, 7, 1), date(2026, 7, 2), Decimal("101.70")),
                (date(2026, 7, 4), date(2026, 7, 4), Decimal("101.70")),
                (date(2026, 7, 5), date(2026, 7, 5), Decimal("90.00")),
            ],
        )


class R4DerivedSyncTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.units = {}
        for code in ("R1", "R2", "R4"):
            self.units[code] = Unit.objects.create(
                tenant=self.tenant,
                property=self.property,
                code=code,
                name=f"Room {code}",
            )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "environment": "production",
                "base_url": "https://app.channex.io/api/v1",
                "property_id": "prop-uuid",
                "certification_property_slug": "uzorita",
                "use_generated_ari": True,
                "booking_test_rooms": [],
            }
        )
        self.integration.save()

        self.plans: dict[tuple[str, str], ChannelRatePlan] = {}
        for code, channex_rp in (
            ("R1", "rp-r1"),
            ("R2", "rp-r2"),
            ("R4", "rp-r4"),
        ):
            for sales_channel in (SalesChannel.BOOKING_COM, SalesChannel.DIRECT):
                plan = ChannelRatePlan.objects.create(
                    tenant=self.tenant,
                    property=self.property,
                    unit=self.units[code],
                    sales_channel=sales_channel,
                    code="standard",
                    title="Standard Rate",
                    channex_room_type_id=f"rt-{code}" if sales_channel == SalesChannel.BOOKING_COM else "",
                    channex_rate_plan_id=(
                        channex_rp if sales_channel == SalesChannel.BOOKING_COM else ""
                    ),
                    default_rate=Decimal("100"),
                    currency="EUR",
                )
                self.plans[(code, sales_channel)] = plan

    def _seed_source(self, sales_channel: str, rows: dict[str, dict[date, Decimal]]):
        for unit_code, day_rates in rows.items():
            plan = self.plans[(unit_code, sales_channel)]
            for day, rate in day_rates.items():
                RatePlanDay.objects.create(
                    tenant=self.tenant,
                    rate_plan=plan,
                    date=day,
                    rate=rate,
                )

    def test_sync_writes_r4_at_ninety_percent(self):
        self._seed_source(
            SalesChannel.BOOKING_COM,
            {
                "R1": {
                    date(2026, 7, 1): Decimal("113.00"),
                    date(2026, 7, 2): Decimal("113.00"),
                },
                "R2": {
                    date(2026, 7, 1): Decimal("113.00"),
                },
            },
        )
        self._seed_source(
            SalesChannel.DIRECT,
            {
                "R1": {date(2026, 7, 1): Decimal("113.00")},
            },
        )

        results = sync_r4_rates_from_king_band(
            tenant_slug="uzorita",
            property_slug="uzorita",
            queue_push=True,
        )
        by_channel = {row.sales_channel: row for row in results}
        self.assertEqual(by_channel[SalesChannel.BOOKING_COM].created, 2)
        self.assertEqual(by_channel[SalesChannel.DIRECT].created, 1)

        r4_booking = {
            row.date: row.rate
            for row in RatePlanDay.objects.filter(
                rate_plan=self.plans[("R4", SalesChannel.BOOKING_COM)]
            )
        }
        self.assertEqual(r4_booking[date(2026, 7, 1)], Decimal("101.70"))
        self.assertEqual(r4_booking[date(2026, 7, 2)], Decimal("101.70"))

        outbox = ChannexAriOutbox.objects.get(
            kind=ChannexAriOutbox.Kind.RESTRICTIONS,
            status=ChannexAriOutbox.Status.PENDING,
        )
        # Compressed Jul 1–2 same rate → one value; push uses OBP normal (101.70+5).
        self.assertEqual(len(outbox.values), 1)
        self.assertEqual(outbox.values[0]["rate"], "106.70")
        self.assertEqual(outbox.values[0]["date_from"], "2026-07-01")
        self.assertEqual(outbox.values[0]["date_to"], "2026-07-02")

    def test_sync_falls_back_to_r2_when_r1_missing(self):
        self._seed_source(
            SalesChannel.BOOKING_COM,
            {"R2": {date(2026, 8, 4): Decimal("452.00")}},
        )
        sync_r4_rates_from_king_band(
            tenant_slug="uzorita",
            sales_channels=(SalesChannel.BOOKING_COM,),
            queue_push=False,
        )
        row = RatePlanDay.objects.get(
            rate_plan=self.plans[("R4", SalesChannel.BOOKING_COM)],
            date=date(2026, 8, 4),
        )
        self.assertEqual(row.rate, Decimal("406.80"))

    def test_dry_run_does_not_write(self):
        self._seed_source(
            SalesChannel.BOOKING_COM,
            {"R1": {date(2026, 7, 1): Decimal("113.00")}},
        )
        results = sync_r4_rates_from_king_band(
            tenant_slug="uzorita",
            sales_channels=(SalesChannel.BOOKING_COM,),
            dry_run=True,
        )
        self.assertEqual(results[0].created, 1)
        self.assertEqual(results[0].written, 0)
        self.assertFalse(
            RatePlanDay.objects.filter(
                rate_plan=self.plans[("R4", SalesChannel.BOOKING_COM)]
            ).exists()
        )

    def test_management_command(self):
        self._seed_source(
            SalesChannel.BOOKING_COM,
            {"R1": {date(2026, 7, 1): Decimal("113.00")}},
        )
        stdout = StringIO()
        call_command(
            "seed_uzorita_r4_rates",
            "--sales-channel",
            SalesChannel.BOOKING_COM,
            "--no-push",
            stdout=stdout,
        )
        row = RatePlanDay.objects.get(
            rate_plan=self.plans[("R4", SalesChannel.BOOKING_COM)],
            date=date(2026, 7, 1),
        )
        self.assertEqual(row.rate, Decimal("101.70"))
        self.assertIn("created=1", stdout.getvalue())
