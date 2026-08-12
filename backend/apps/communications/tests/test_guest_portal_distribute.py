"""PR-C: guest portal link distribution after web check-in."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_compose import (
    HINT_ASK_ARRIVAL_TIME,
    HINT_GUEST_PORTAL_LINK,
    HINT_GUEST_PORTAL_LINK_URL,
    render_guest_portal_link_email_html,
    render_guest_portal_link_message,
    render_guest_portal_link_url_only,
)
from apps.communications.guest_portal_distribute import (
    portal_link_already_sent,
    resolve_portal_link_channel,
    send_guest_portal_link,
    send_guest_portal_link_for_session,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
    PostCheckinSendClaim,
    PostCheckinSendClaimStatus,
)
from apps.communications.post_checkin_claims import (
    arrival_ask_claim_key,
    portal_claim_key,
    try_acquire_claim,
)
from apps.reservations.guest_portal_access import ensure_active_portal_access
from apps.properties.models import Property
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_session import ensure_active_session, mark_session_completed
from apps.reservations.models import (
    Guest,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    GuestPortalAccess,
    Reservation,
)
from apps.tenants.models import Tenant


class ResolvePortalLinkChannelTests(TestCase):
    def test_channel_map(self):
        self.assertEqual(
            resolve_portal_link_channel(GuestCheckInSessionCreatedFrom.CHANNEX),
            GuestMessageChannel.BOOKING,
        )
        self.assertEqual(
            resolve_portal_link_channel(GuestCheckInSessionCreatedFrom.EMAIL),
            GuestMessageChannel.EMAIL,
        )
        self.assertEqual(
            resolve_portal_link_channel(GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN),
            GuestMessageChannel.WHATSAPP,
        )
        self.assertEqual(
            resolve_portal_link_channel(GuestCheckInSessionCreatedFrom.RECEPTION_MANUAL),
            GuestMessageChannel.EMAIL,
        )
        self.assertIsNone(resolve_portal_link_channel("unknown"))


class GuestPortalComposeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="gp-compose", name="GP Compose")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Compose Property",
            slug="compose",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-C-1",
            check_in=date(2026, 7, 21),
            check_out=date(2026, 7, 23),
            booker_name="Ada",
            booker_country="GB",
            status=Reservation.Status.EXPECTED,
        )

    def test_cta_message_includes_portal_url(self):
        url = "https://booking.example.test/g/tok"
        text = render_guest_portal_link_message(self.reservation, portal_url=url)
        self.assertIn("/g/tok", text)
        self.assertIn("?lang=", text)
        self.assertIn(url, text)
        self.assertIn("arrival", text.lower())
        self.assertIn("stay.hr", text)

    def test_url_only_is_localized_link(self):
        url = "https://booking.example.test/g/tok"
        text = render_guest_portal_link_url_only(self.reservation, portal_url=url)
        self.assertTrue(text.startswith(url))
        self.assertIn("?lang=", text)
        self.assertEqual(text.strip(), text)
        self.assertNotIn("\n", text)

    def test_email_html_keeps_button_plain_body_has_url(self):
        url = "https://booking.example.test/g/tok"
        html = render_guest_portal_link_email_html(self.reservation, portal_url=url)
        plain = render_guest_portal_link_message(self.reservation, portal_url=url)
        self.assertIn("href=", html)
        self.assertIn(url, html)
        # HTML CTA paragraph itself is label-only; URL is in the button href.
        plain_p = html.split("<p>")[1].split("</p>")[0]
        self.assertNotIn("http", plain_p.lower())
        self.assertIn("arrival", plain_p.lower())
        self.assertIn("/g/tok", plain)
        self.assertIn("?lang=", plain)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class GuestPortalDistributeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="gp-dist", name="GP Dist")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Dist Property",
            slug="dist",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-D-1",
            check_in=date(2026, 7, 21),
            check_out=date(2026, 7, 24),
            adults_count=1,
            booker_name="Portal Guest",
            booker_email="guest@example.com",
            booker_phone="+385911111111",
            amount=Decimal("90.00"),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Portal",
            last_name="Guest",
            name="Portal Guest",
            is_primary=True,
        )

    def _complete_session(self, created_from: str):
        session = ensure_active_session(self.reservation, created_from=created_from)
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def test_email_channel_uses_email_not_whatsapp(self):
        session = self._complete_session(GuestCheckInSessionCreatedFrom.EMAIL)
        sent_channels: list[str] = []

        def fake_email(*args, **kwargs):
            draft = kwargs.get("draft")
            if draft is not None:
                draft.channel = GuestMessageChannel.EMAIL
                draft.final_body_text = args[1] if len(args) > 1 else ""
                draft.save(update_fields=["channel", "final_body_text"])
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            sent_channels.append(GuestMessageChannel.EMAIL)
            return outbound

        with (
            patch(
                "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
                side_effect=fake_email,
            ) as mock_email,
            patch(
                "apps.communications.guest_portal_distribute.send_guest_message",
            ) as mock_send,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["channel"], GuestMessageChannel.EMAIL)
        self.assertEqual(mock_email.call_count, 2)
        mock_send.assert_not_called()
        self.assertEqual(sent_channels, [GuestMessageChannel.EMAIL, GuestMessageChannel.EMAIL])
        self.assertEqual(result.get("arrival_ask_status"), "sent")
        self.assertTrue(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_GUEST_PORTAL_LINK,
            ).exists()
        )
        self.assertTrue(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_ASK_ARRIVAL_TIME,
            ).exists()
        )
        self.assertTrue(
            GuestPortalAccess.objects.filter(reservation=self.reservation).exists()
        )

    def test_channex_uses_booking_single_send_with_url(self):
        session = self._complete_session(GuestCheckInSessionCreatedFrom.CHANNEX)
        bodies: list[str] = []

        def fake_send(*, channel, **kwargs):
            draft = kwargs["draft"]
            draft.channel = channel
            draft.final_body_text = kwargs["body_text"]
            bodies.append(kwargs["body_text"])
            from django.utils import timezone

            draft.sent_at = timezone.now()
            draft.save(update_fields=["channel", "final_body_text", "sent_at"])
            return MagicMock(delivery_status="sent")

        with (
            patch(
                "apps.communications.guest_portal_distribute.send_guest_message",
                side_effect=fake_send,
            ) as mock_send,
            patch(
                "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            ) as mock_email,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["channel"], GuestMessageChannel.BOOKING)
        # Portal (CTA+URL) + arrival ask
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(
            mock_send.call_args_list[0].kwargs["channel"],
            GuestMessageChannel.BOOKING,
        )
        self.assertEqual(
            mock_send.call_args_list[1].kwargs["channel"],
            GuestMessageChannel.BOOKING,
        )
        mock_email.assert_not_called()
        self.assertIn("/g/", bodies[0])
        self.assertIn("?lang=", bodies[0])
        self.assertEqual(result.get("arrival_ask_status"), "sent")
        self.assertTrue(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_GUEST_PORTAL_LINK,
            ).exists()
        )
        self.assertFalse(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_GUEST_PORTAL_LINK_URL,
            ).exists()
        )
        self.assertTrue(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_ASK_ARRIVAL_TIME,
            ).exists()
        )
        self.assertNotIn("url_draft_id", result)

    def test_whatsapp_autocheckin_uses_whatsapp_single_send_with_url(self):
        session = self._complete_session(
            GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        )
        bodies: list[str] = []

        def fake_send(*, channel, **kwargs):
            draft = kwargs["draft"]
            draft.channel = channel
            draft.final_body_text = kwargs["body_text"]
            bodies.append(kwargs["body_text"])
            from django.utils import timezone

            draft.sent_at = timezone.now()
            draft.save(update_fields=["channel", "final_body_text", "sent_at"])
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_send,
        ) as mock_send:
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["channel"], GuestMessageChannel.WHATSAPP)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(
            mock_send.call_args_list[0].kwargs["channel"],
            GuestMessageChannel.WHATSAPP,
        )
        self.assertEqual(result.get("arrival_ask_status"), "sent")
        self.assertIn("/g/", bodies[0])
        self.assertIn("?lang=", bodies[0])

    def test_channex_portal_fail_skips_arrival_ask(self):
        session = self._complete_session(GuestCheckInSessionCreatedFrom.CHANNEX)

        def fake_send(*, channel, **kwargs):
            raise RuntimeError("provider boom")

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_send,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("draft_id", result)
        self.assertIn("provider boom", result["error"])
        self.assertNotIn("arrival_ask_status", result)
        self.assertFalse(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_ASK_ARRIVAL_TIME,
            ).exists()
        )
        self.assertTrue(portal_link_already_sent(self.reservation))

    def test_portal_send_failed_skips_arrival_ask(self):
        session = self._complete_session(GuestCheckInSessionCreatedFrom.EMAIL)

        def fail_email(*args, **kwargs):
            raise RuntimeError("smtp boom")

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fail_email,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("arrival_ask_status", result)
        self.assertFalse(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_ASK_ARRIVAL_TIME,
            ).exists()
        )

    def test_portal_sent_skips_ask_when_arrival_already_stated(self):
        self.reservation.guest_stated_arrival_text = "around 16:00"
        self.reservation.save(update_fields=["guest_stated_arrival_text", "updated_at"])
        session = self._complete_session(GuestCheckInSessionCreatedFrom.EMAIL)

        def fake_email(*args, **kwargs):
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ) as mock_email:
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(mock_email.call_count, 1)
        self.assertEqual(result.get("arrival_ask_status"), "skipped")
        self.assertEqual(result.get("arrival_ask_reason"), "already_stated")

    def test_reception_manual_without_email_skips(self):
        self.reservation.booker_email = ""
        self.reservation.save(update_fields=["booker_email", "updated_at"])
        session = self._complete_session(
            GuestCheckInSessionCreatedFrom.RECEPTION_MANUAL,
        )
        result = send_guest_portal_link_for_session(
            reservation_id=self.reservation.pk,
            session_id=session.pk,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_email")
        self.assertFalse(portal_link_already_sent(self.reservation))

    def test_unsent_portal_draft_does_not_block_retry(self):
        """G3: draft/failed existence alone is not a dedup hit."""
        session = self._complete_session(GuestCheckInSessionCreatedFrom.EMAIL)
        GuestMessageDraft.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            intent=GuestMessageIntent.CHECKIN,
            hint=HINT_GUEST_PORTAL_LINK,
            llm_body_text="failed attempt draft",
            final_body_text="failed attempt draft",
            language="en",
            channel=GuestMessageChannel.EMAIL,
        )

        def fake_email(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            if draft is not None:
                draft.channel = GuestMessageChannel.EMAIL
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=GuestMessageChannel.EMAIL,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_email=self.reservation.booker_email,
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ) as mock_email:
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertGreaterEqual(mock_email.call_count, 1)


class GuestPortalG3DedupTests(TestCase):
    """G3: success-only portal dedup for session + current token + channel."""

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-g3", name="GP G3")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="G3 Property",
            slug="g3",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-G3-1",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="G3 Guest",
            booker_email="g3@example.com",
            booker_phone="+385922222222",
            amount=Decimal("80.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="G3",
            last_name="Guest",
            name="G3 Guest",
            is_primary=True,
        )

    def _complete_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def _fake_email_persist(self, *, fail_portal: bool = False):
        state = {"portal_attempts": 0}

        def fake_email(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_GUEST_PORTAL_LINK:
                state["portal_attempts"] += 1
                if fail_portal and state["portal_attempts"] == 1:
                    if draft is not None:
                        GuestOutboundMessage.objects.create(
                            tenant_id=self.tenant.pk,
                            reservation=self.reservation,
                            draft=draft,
                            channel=GuestMessageChannel.EMAIL,
                            body_text=body,
                            status=GuestOutboundMessageStatus.FAILED,
                            error_message="send_failed",
                            to_email=self.reservation.booker_email,
                        )
                    raise RuntimeError("smtp boom")

            if draft is not None:
                draft.channel = GuestMessageChannel.EMAIL
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=GuestMessageChannel.EMAIL,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_email=self.reservation.booker_email,
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        return fake_email, state

    def _portal_sent_count(self) -> int:
        return GuestOutboundMessage.objects.filter(
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            draft__hint=HINT_GUEST_PORTAL_LINK,
            status=GuestOutboundMessageStatus.SENT,
        ).count()

    def _ask_sent_count(self) -> int:
        return GuestOutboundMessage.objects.filter(
            reservation=self.reservation,
            draft__hint=HINT_ASK_ARRIVAL_TIME,
            status=GuestOutboundMessageStatus.SENT,
        ).count()

    def test_g3_second_complete_skips_duplicate_portal_and_ask(self):
        """complete #1 sent → complete #2: no second portal, no second ask."""
        session = self._complete_session()
        fake_email, _state = self._fake_email_persist()

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            second = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(first["status"], "sent")
        self.assertEqual(first.get("arrival_ask_status"), "sent")
        self.assertEqual(self._portal_sent_count(), 1)
        self.assertEqual(self._ask_sent_count(), 1)

        self.assertIn(second["status"], {"already_sent", "idempotent_skip", "sent"})
        self.assertEqual(self._portal_sent_count(), 1)
        self.assertEqual(self._ask_sent_count(), 1)
        self.assertEqual(second.get("arrival_ask_status"), "skipped")
        self.assertEqual(second.get("arrival_ask_reason"), "already_sent")

    def test_g3_failed_portal_allows_retry_then_dedups(self):
        """failed → retry allowed → sent → third attempt dedup skip."""
        session = self._complete_session()
        fake_email, state = self._fake_email_persist(fail_portal=True)

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(first["status"], "failed")
            self.assertEqual(self._portal_sent_count(), 0)
            self.assertEqual(self._ask_sent_count(), 0)

            second = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(second["status"], "sent")
            self.assertEqual(second.get("arrival_ask_status"), "sent")
            self.assertEqual(self._portal_sent_count(), 1)
            self.assertEqual(self._ask_sent_count(), 1)

            third = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(self._portal_sent_count(), 1)
        self.assertEqual(self._ask_sent_count(), 1)
        self.assertEqual(third.get("arrival_ask_status"), "skipped")
        self.assertEqual(state["portal_attempts"], 2)

    def test_g3_allow_resend_bypasses_success_dedup(self):
        """allow_resend=True sends another portal even after a successful send."""
        session = self._complete_session()
        fake_email, _state = self._fake_email_persist()

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(first["status"], "sent")
            self.assertEqual(self._portal_sent_count(), 1)

            resent = send_guest_portal_link(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                allow_resend=True,
                session_id=session.pk,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )

        self.assertEqual(resent["status"], "sent")
        self.assertEqual(self._portal_sent_count(), 2)


class GuestPortalG4ArrivalAskDedupTests(TestCase):
    """G4: arrival-ask success-only dedup per session (not per channel)."""

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-g4", name="GP G4")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="G4 Property",
            slug="g4",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-G4-1",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="G4 Guest",
            booker_email="g4@example.com",
            booker_phone="+385933333333",
            amount=Decimal("70.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="G4",
            last_name="Guest",
            name="G4 Guest",
            is_primary=True,
        )

    def _complete_wa_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def _complete_email_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def _persist_send(self, *, channel: str, fail_ask_once: bool = False):
        state = {"ask_attempts": 0}

        def fake_send(*args, **kwargs):
            # email path: (reservation, body, ..., draft=)
            # whatsapp path: reservation=, draft=, channel=, body_text=
            draft = kwargs.get("draft")
            body = kwargs.get("body_text")
            if body is None and len(args) > 1:
                body = args[1]
            body = body or ""
            ch = kwargs.get("channel") or channel
            hint = getattr(draft, "hint", "") if draft is not None else ""

            if hint == HINT_ASK_ARRIVAL_TIME:
                state["ask_attempts"] += 1
                if fail_ask_once and state["ask_attempts"] == 1:
                    if draft is not None:
                        GuestOutboundMessage.objects.create(
                            tenant_id=self.tenant.pk,
                            reservation=self.reservation,
                            draft=draft,
                            channel=ch,
                            body_text=body,
                            status=GuestOutboundMessageStatus.FAILED,
                            error_message="ask_failed",
                            to_email=self.reservation.booker_email or "",
                            to_phone=self.reservation.booker_phone or "",
                        )
                    raise RuntimeError("ask boom")

            if draft is not None:
                draft.channel = ch
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=ch,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_email=self.reservation.booker_email or "",
                    to_phone=self.reservation.booker_phone or "",
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        return fake_send, state

    def _ask_sent_count(self) -> int:
        return GuestOutboundMessage.objects.filter(
            reservation=self.reservation,
            draft__hint=HINT_ASK_ARRIVAL_TIME,
            status=GuestOutboundMessageStatus.SENT,
        ).count()

    def test_g4_retry_skips_second_ask_after_success(self):
        session = self._complete_email_session()
        fake_email, _state = self._persist_send(channel=GuestMessageChannel.EMAIL)

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            second = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(first.get("arrival_ask_status"), "sent")
        self.assertEqual(self._ask_sent_count(), 1)
        self.assertEqual(second.get("arrival_ask_status"), "skipped")
        self.assertEqual(second.get("arrival_ask_reason"), "already_sent")
        self.assertEqual(self._ask_sent_count(), 1)

    def test_g4_failed_ask_allows_retry(self):
        session = self._complete_email_session()
        fake_email, state = self._persist_send(
            channel=GuestMessageChannel.EMAIL,
            fail_ask_once=True,
        )

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(first["status"], "sent")
            self.assertEqual(first.get("arrival_ask_status"), "failed")
            self.assertEqual(self._ask_sent_count(), 0)

            second = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(second["status"], "already_sent")
        self.assertEqual(second.get("arrival_ask_status"), "sent")
        self.assertEqual(self._ask_sent_count(), 1)
        self.assertEqual(state["ask_attempts"], 2)

    def test_g4_ask_dedup_is_session_not_channel(self):
        """Ask sent on WhatsApp must not be resent when later path is email."""
        session = self._complete_wa_session()
        fake_wa, _ = self._persist_send(channel=GuestMessageChannel.WHATSAPP)
        fake_email, _ = self._persist_send(channel=GuestMessageChannel.EMAIL)

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_wa,
        ):
            wa_result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(wa_result["status"], "sent")
        self.assertEqual(wa_result.get("arrival_ask_status"), "sent")
        self.assertEqual(self._ask_sent_count(), 1)

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            email_result = send_guest_portal_link(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                allow_resend=True,
                session_id=session.pk,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )

        self.assertEqual(email_result["status"], "sent")
        self.assertEqual(email_result.get("arrival_ask_status"), "skipped")
        self.assertEqual(email_result.get("arrival_ask_reason"), "already_sent")
        self.assertEqual(self._ask_sent_count(), 1)


class GuestPortalG5ClaimTests(TestCase):
    """G5: UNIQUE claim_key is the concurrency gate; failed is reclaimable."""

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-g5", name="GP G5")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="G5 Property",
            slug="g5",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-G5-1",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="G5 Guest",
            booker_email="g5@example.com",
            booker_phone="+385944444444",
            amount=Decimal("90.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="G5",
            last_name="Guest",
            name="G5 Guest",
            is_primary=True,
        )

    def _complete_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def _portal_key(self, session):
        access = ensure_active_portal_access(self.reservation)
        return portal_claim_key(
            session_id=session.pk,
            portal_token=access.token,
            channel=GuestMessageChannel.EMAIL,
        )

    def _fake_email(self, *, hang_on_portal: bool = False):
        state = {"portal_attempts": 0, "ask_attempts": 0}

        def fake_email(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_GUEST_PORTAL_LINK:
                state["portal_attempts"] += 1
                if hang_on_portal:
                    raise AssertionError("provider must not run while claim pending")
            if hint == HINT_ASK_ARRIVAL_TIME:
                state["ask_attempts"] += 1
            if draft is not None:
                draft.channel = GuestMessageChannel.EMAIL
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=GuestMessageChannel.EMAIL,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_email=self.reservation.booker_email,
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        return fake_email, state

    def test_g5_try_acquire_blocks_pending_and_sent_reclaims_failed(self):
        session = self._complete_session()
        key = self._portal_key(session)

        first = try_acquire_claim(claim_key=key, reservation=self.reservation)
        self.assertIsNotNone(first.claim)
        self.assertEqual(first.claim.status, PostCheckinSendClaimStatus.PENDING)

        blocked = try_acquire_claim(claim_key=key, reservation=self.reservation)
        self.assertIsNone(blocked.claim)
        self.assertEqual(blocked.blocked_status, PostCheckinSendClaimStatus.PENDING)

        first.claim.status = PostCheckinSendClaimStatus.SENT
        first.claim.save(update_fields=["status", "updated_at"])
        blocked_sent = try_acquire_claim(claim_key=key, reservation=self.reservation)
        self.assertIsNone(blocked_sent.claim)
        self.assertEqual(blocked_sent.blocked_status, PostCheckinSendClaimStatus.SENT)

        first.claim.status = PostCheckinSendClaimStatus.FAILED
        first.claim.save(update_fields=["status", "updated_at"])
        reclaim = try_acquire_claim(claim_key=key, reservation=self.reservation)
        self.assertIsNotNone(reclaim.claim)
        self.assertEqual(reclaim.claim.status, PostCheckinSendClaimStatus.PENDING)

    def test_g5_pending_claim_blocks_parallel_portal_without_provider(self):
        session = self._complete_session()
        PostCheckinSendClaim.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            claim_key=self._portal_key(session),
            status=PostCheckinSendClaimStatus.PENDING,
        )
        fake_email, state = self._fake_email(hang_on_portal=True)

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result.get("reason"), "claim_pending")
        self.assertEqual(state["portal_attempts"], 0)

    def test_g5_failed_claim_allows_retry_then_marks_sent(self):
        session = self._complete_session()
        claim = PostCheckinSendClaim.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            claim_key=self._portal_key(session),
            status=PostCheckinSendClaimStatus.FAILED,
        )
        fake_email, state = self._fake_email()

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result.get("arrival_ask_status"), "sent")
        self.assertEqual(state["portal_attempts"], 1)
        claim.refresh_from_db()
        self.assertEqual(claim.status, PostCheckinSendClaimStatus.SENT)
        ask = PostCheckinSendClaim.objects.get(
            claim_key=arrival_ask_claim_key(session_id=session.pk),
        )
        self.assertEqual(ask.status, PostCheckinSendClaimStatus.SENT)

    def test_g5_provider_fail_marks_claim_failed_for_retry(self):
        session = self._complete_session()
        attempts = {"n": 0}

        def boom_then_ok(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_GUEST_PORTAL_LINK:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("smtp boom")
            if draft is not None:
                draft.channel = GuestMessageChannel.EMAIL
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=GuestMessageChannel.EMAIL,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_email=self.reservation.booker_email,
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=boom_then_ok,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(first["status"], "failed")
            claim = PostCheckinSendClaim.objects.get(
                claim_key=self._portal_key(session),
            )
            self.assertEqual(claim.status, PostCheckinSendClaimStatus.FAILED)

            second = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(second["status"], "sent")
        self.assertEqual(second.get("arrival_ask_status"), "sent")
        claim.refresh_from_db()
        self.assertEqual(claim.status, PostCheckinSendClaimStatus.SENT)
        self.assertEqual(attempts["n"], 2)

    def test_g5_allow_resend_bypasses_portal_claim(self):
        session = self._complete_session()
        PostCheckinSendClaim.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            claim_key=self._portal_key(session),
            status=PostCheckinSendClaimStatus.PENDING,
        )
        fake_email, state = self._fake_email()

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            result = send_guest_portal_link(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                allow_resend=True,
                session_id=session.pk,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(state["portal_attempts"], 1)
        # Operator bypass must not flip the other worker's pending claim.
        claim = PostCheckinSendClaim.objects.get(claim_key=self._portal_key(session))
        self.assertEqual(claim.status, PostCheckinSendClaimStatus.PENDING)

    def test_g5_ask_pending_claim_blocks_without_provider(self):
        session = self._complete_session()
        fake_email, state = self._fake_email()
        PostCheckinSendClaim.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            claim_key=arrival_ask_claim_key(session_id=session.pk),
            status=PostCheckinSendClaimStatus.PENDING,
        )

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result.get("arrival_ask_status"), "skipped")
        self.assertEqual(result.get("arrival_ask_reason"), "claim_pending")
        self.assertEqual(state["ask_attempts"], 0)


class GuestPortalG6StickyChannelTests(TestCase):
    """G6: arrival ask uses successful portal outbound channel (sticky)."""

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-g6", name="GP G6")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="G6 Property",
            slug="g6",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-G6-1",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="G6 Guest",
            booker_email="g6@example.com",
            booker_phone="+385955555555",
            amount=Decimal("95.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="G6",
            last_name="Guest",
            name="G6 Guest",
            is_primary=True,
        )

    def _complete_wa_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def test_g6_ask_stays_on_portal_channel_if_last_distributed_flips(self):
        """Portal WA success → last_distributed_from=email before ask → ask=WA."""
        from apps.communications.guest_portal_distribute import (
            resolve_sticky_arrival_ask_channel,
        )

        session = self._complete_wa_session()
        state = {"ask_channels": []}

        def fake_send(*args, **kwargs):
            draft = kwargs.get("draft")
            body = kwargs.get("body_text") or ""
            ch = kwargs.get("channel") or GuestMessageChannel.WHATSAPP
            hint = getattr(draft, "hint", "") if draft is not None else ""

            if hint == HINT_GUEST_PORTAL_LINK:
                if draft is not None:
                    draft.channel = ch
                    draft.final_body_text = body
                    draft.sent_at = timezone.now()
                    draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                    GuestOutboundMessage.objects.create(
                        tenant_id=self.tenant.pk,
                        reservation=self.reservation,
                        draft=draft,
                        channel=ch,
                        body_text=body,
                        status=GuestOutboundMessageStatus.SENT,
                        to_phone=self.reservation.booker_phone,
                    )
                # Simulate session stamp flipping before ask phase.
                session.last_distributed_from = GuestCheckInSessionCreatedFrom.EMAIL
                session.save(update_fields=["last_distributed_from", "updated_at"])

            if hint == HINT_ASK_ARRIVAL_TIME:
                state["ask_channels"].append(ch)
                if draft is not None:
                    draft.channel = ch
                    draft.final_body_text = body
                    draft.sent_at = timezone.now()
                    draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                    GuestOutboundMessage.objects.create(
                        tenant_id=self.tenant.pk,
                        reservation=self.reservation,
                        draft=draft,
                        channel=ch,
                        body_text=body,
                        status=GuestOutboundMessageStatus.SENT,
                        to_phone=self.reservation.booker_phone,
                    )

            if hint == HINT_GUEST_PORTAL_LINK_URL:
                if draft is not None:
                    draft.channel = ch
                    draft.final_body_text = body
                    draft.sent_at = timezone.now()
                    draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                    GuestOutboundMessage.objects.create(
                        tenant_id=self.tenant.pk,
                        reservation=self.reservation,
                        draft=draft,
                        channel=ch,
                        body_text=body,
                        status=GuestOutboundMessageStatus.SENT,
                        to_phone=self.reservation.booker_phone,
                    )

            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_send,
        ):
            result = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )

        session.refresh_from_db()
        self.assertEqual(
            session.last_distributed_from,
            GuestCheckInSessionCreatedFrom.EMAIL,
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result.get("arrival_ask_status"), "sent")
        self.assertEqual(
            result.get("arrival_ask_channel"),
            GuestMessageChannel.WHATSAPP,
        )
        self.assertEqual(state["ask_channels"], [GuestMessageChannel.WHATSAPP])

        access = ensure_active_portal_access(self.reservation)
        sticky = resolve_sticky_arrival_ask_channel(
            self.reservation,
            portal_token=access.token,
            session_id=session.pk,
            fallback_channel=GuestMessageChannel.EMAIL,
        )
        self.assertEqual(sticky, GuestMessageChannel.WHATSAPP)

    def test_g6_already_sent_ask_uses_existing_portal_channel(self):
        """Existing WA portal + email fallback channel → ask still WhatsApp."""
        from apps.communications.guest_portal_distribute import (
            _maybe_send_arrival_ask_after_portal,
        )

        session = self._complete_wa_session()
        state = {"ask_channels": [], "portal_attempts": 0}

        def fake_send(*args, **kwargs):
            draft = kwargs.get("draft")
            body = kwargs.get("body_text") or ""
            ch = kwargs.get("channel") or GuestMessageChannel.WHATSAPP
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_GUEST_PORTAL_LINK:
                state["portal_attempts"] += 1
            if hint == HINT_ASK_ARRIVAL_TIME:
                state["ask_channels"].append(ch)
                # Fail first ask so retry path can exercise sticky.
                if len(state["ask_channels"]) == 1:
                    raise RuntimeError("ask boom")
            if draft is not None and hint != HINT_ASK_ARRIVAL_TIME:
                draft.channel = ch
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=ch,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_phone=self.reservation.booker_phone,
                )
            if draft is not None and hint == HINT_ASK_ARRIVAL_TIME:
                draft.channel = ch
                draft.final_body_text = body
                draft.sent_at = timezone.now()
                draft.save(update_fields=["channel", "final_body_text", "sent_at"])
                GuestOutboundMessage.objects.create(
                    tenant_id=self.tenant.pk,
                    reservation=self.reservation,
                    draft=draft,
                    channel=ch,
                    body_text=body,
                    status=GuestOutboundMessageStatus.SENT,
                    to_phone=self.reservation.booker_phone,
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_send,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(first["status"], "sent")
            self.assertEqual(first.get("arrival_ask_status"), "failed")

            session.last_distributed_from = GuestCheckInSessionCreatedFrom.EMAIL
            session.save(update_fields=["last_distributed_from", "updated_at"])

            access = ensure_active_portal_access(self.reservation)
            # Ask gate with email fallback — sticky must keep WhatsApp.
            ask = _maybe_send_arrival_ask_after_portal(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                base={"status": "already_sent"},
                session_id=session.pk,
                portal_token=access.token,
            )

        self.assertEqual(ask.get("arrival_ask_status"), "sent")
        self.assertEqual(ask.get("arrival_ask_channel"), GuestMessageChannel.WHATSAPP)
        self.assertEqual(
            state["ask_channels"],
            [GuestMessageChannel.WHATSAPP, GuestMessageChannel.WHATSAPP],
        )
        self.assertEqual(state["portal_attempts"], 1)


class GuestPortalG7LatestSuccessfulOwnershipTests(TestCase):
    """G7: latest successful portal owns orphan ask; failed resend ignored."""

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-g7", name="GP G7")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="G7 Property",
            slug="g7",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-G7-1",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="G7 Guest",
            booker_email="g7@example.com",
            booker_phone="+385966666666",
            amount=Decimal("100.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="G7",
            last_name="Guest",
            name="G7 Guest",
            is_primary=True,
        )

    def _complete_wa_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def _persist(self, *, channel: str, body: str, draft, status: str):
        if draft is None:
            return
        if status == GuestOutboundMessageStatus.SENT:
            draft.channel = channel
            draft.final_body_text = body
            draft.sent_at = timezone.now()
            draft.save(update_fields=["channel", "final_body_text", "sent_at"])
        GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            draft=draft,
            channel=channel,
            body_text=body,
            status=status,
            to_email=self.reservation.booker_email or "",
            to_phone=self.reservation.booker_phone or "",
            error_message="" if status == GuestOutboundMessageStatus.SENT else "boom",
        )

    def test_g7_successful_email_resend_owns_orphan_ask(self):
        """WA portal SUCCESS + ask FAIL → email allow_resend SUCCESS → ask=Email."""
        session = self._complete_wa_session()
        ask_channels: list[str] = []

        def fake_wa(*args, **kwargs):
            draft = kwargs.get("draft")
            body = kwargs.get("body_text") or ""
            ch = kwargs.get("channel") or GuestMessageChannel.WHATSAPP
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_channels.append(ch)
                self._persist(
                    channel=ch,
                    body=body,
                    draft=draft,
                    status=GuestOutboundMessageStatus.FAILED,
                )
                raise RuntimeError("ask boom")
            self._persist(
                channel=ch,
                body=body,
                draft=draft,
                status=GuestOutboundMessageStatus.SENT,
            )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        def fake_email(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_channels.append(GuestMessageChannel.EMAIL)
            self._persist(
                channel=GuestMessageChannel.EMAIL,
                body=body,
                draft=draft,
                status=GuestOutboundMessageStatus.SENT,
            )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_wa,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
        self.assertEqual(first["status"], "sent")
        self.assertEqual(first.get("arrival_ask_status"), "failed")
        self.assertEqual(ask_channels, [GuestMessageChannel.WHATSAPP])

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            resent = send_guest_portal_link(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                allow_resend=True,
                session_id=session.pk,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )

        self.assertEqual(resent["status"], "sent")
        self.assertEqual(resent.get("arrival_ask_status"), "sent")
        self.assertEqual(
            resent.get("arrival_ask_channel"),
            GuestMessageChannel.EMAIL,
        )
        self.assertEqual(
            ask_channels,
            [GuestMessageChannel.WHATSAPP, GuestMessageChannel.EMAIL],
        )

    def test_g7_failed_email_resend_does_not_steal_ask_ownership(self):
        """WA portal SUCCESS + ask FAIL → email allow_resend FAIL → ask retry=WA."""
        from apps.communications.guest_portal_distribute import (
            _maybe_send_arrival_ask_after_portal,
        )

        session = self._complete_wa_session()
        ask_channels: list[str] = []

        def fake_wa(*args, **kwargs):
            draft = kwargs.get("draft")
            body = kwargs.get("body_text") or ""
            ch = kwargs.get("channel") or GuestMessageChannel.WHATSAPP
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_channels.append(ch)
                if len(ask_channels) == 1:
                    self._persist(
                        channel=ch,
                        body=body,
                        draft=draft,
                        status=GuestOutboundMessageStatus.FAILED,
                    )
                    raise RuntimeError("ask boom")
                self._persist(
                    channel=ch,
                    body=body,
                    draft=draft,
                    status=GuestOutboundMessageStatus.SENT,
                )
            else:
                self._persist(
                    channel=ch,
                    body=body,
                    draft=draft,
                    status=GuestOutboundMessageStatus.SENT,
                )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        def fake_email_fail(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_GUEST_PORTAL_LINK:
                self._persist(
                    channel=GuestMessageChannel.EMAIL,
                    body=body,
                    draft=draft,
                    status=GuestOutboundMessageStatus.FAILED,
                )
                raise RuntimeError("smtp boom")
            raise AssertionError(f"unexpected email hint={hint}")

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_wa,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
            self.assertEqual(first["status"], "sent")
            self.assertEqual(first.get("arrival_ask_status"), "failed")

            with patch(
                "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
                side_effect=fake_email_fail,
            ):
                resent = send_guest_portal_link(
                    self.reservation,
                    channel=GuestMessageChannel.EMAIL,
                    allow_resend=True,
                    session_id=session.pk,
                    created_from=GuestCheckInSessionCreatedFrom.EMAIL,
                )
            self.assertEqual(resent["status"], "failed")

            access = ensure_active_portal_access(self.reservation)
            ask = _maybe_send_arrival_ask_after_portal(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                base={"status": "already_sent"},
                session_id=session.pk,
                portal_token=access.token,
            )

        self.assertEqual(ask.get("arrival_ask_status"), "sent")
        self.assertEqual(ask.get("arrival_ask_channel"), GuestMessageChannel.WHATSAPP)
        self.assertEqual(
            ask_channels,
            [GuestMessageChannel.WHATSAPP, GuestMessageChannel.WHATSAPP],
        )


class GuestPortalG8CurrentTokenScopeTests(TestCase):
    """G8: sticky / ask ownership scoped to current portal token only."""

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-g8", name="GP G8")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="G8 Property",
            slug="g8",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-G8-1",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="G8 Guest",
            booker_email="g8@example.com",
            booker_phone="+385977777777",
            amount=Decimal("110.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="G8",
            last_name="Guest",
            name="G8 Guest",
            is_primary=True,
        )

    def _complete_wa_session(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        )
        mark_session_completed(session)
        session.refresh_from_db()
        return session

    def _persist(self, *, channel: str, body: str, draft, status: str):
        if draft is None:
            return
        if status == GuestOutboundMessageStatus.SENT:
            draft.channel = channel
            draft.final_body_text = body
            draft.sent_at = timezone.now()
            draft.save(update_fields=["channel", "final_body_text", "sent_at"])
        GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            draft=draft,
            channel=channel,
            body_text=body,
            status=status,
            to_email=self.reservation.booker_email or "",
            to_phone=self.reservation.booker_phone or "",
            error_message="" if status == GuestOutboundMessageStatus.SENT else "boom",
        )

    def test_g8_ask_follows_new_token_portal_not_old_token(self):
        """Token A WA SUCCESS (ask failed) → regenerate B → email SUCCESS → ask=Email."""
        from apps.communications.guest_portal_distribute import (
            resolve_sticky_arrival_ask_channel,
        )
        from apps.reservations.guest_portal_access import regenerate_portal_access

        session = self._complete_wa_session()
        ask_channels: list[str] = []

        def fake_wa(*args, **kwargs):
            draft = kwargs.get("draft")
            body = kwargs.get("body_text") or ""
            ch = kwargs.get("channel") or GuestMessageChannel.WHATSAPP
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_channels.append(ch)
                self._persist(
                    channel=ch,
                    body=body,
                    draft=draft,
                    status=GuestOutboundMessageStatus.FAILED,
                )
                raise RuntimeError("ask boom")
            self._persist(
                channel=ch,
                body=body,
                draft=draft,
                status=GuestOutboundMessageStatus.SENT,
            )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        def fake_email(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_channels.append(GuestMessageChannel.EMAIL)
            self._persist(
                channel=GuestMessageChannel.EMAIL,
                body=body,
                draft=draft,
                status=GuestOutboundMessageStatus.SENT,
            )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            return outbound

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_wa,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
        self.assertEqual(first["status"], "sent")
        self.assertEqual(first.get("arrival_ask_status"), "failed")
        token_a = ensure_active_portal_access(self.reservation).token

        _old, new_access = regenerate_portal_access(self.reservation)
        self.assertNotEqual(str(new_access.token), str(token_a))

        # Old token must not drive sticky for the new token.
        self.assertEqual(
            resolve_sticky_arrival_ask_channel(
                self.reservation,
                portal_token=new_access.token,
                session_id=session.pk,
                fallback_channel=GuestMessageChannel.EMAIL,
            ),
            GuestMessageChannel.EMAIL,
        )

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email,
        ):
            second = send_guest_portal_link(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                session_id=session.pk,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )

        self.assertEqual(second["status"], "sent")
        self.assertEqual(second.get("arrival_ask_status"), "sent")
        self.assertEqual(
            second.get("arrival_ask_channel"),
            GuestMessageChannel.EMAIL,
        )
        self.assertEqual(
            ask_channels,
            [GuestMessageChannel.WHATSAPP, GuestMessageChannel.EMAIL],
        )
        self.assertEqual(
            resolve_sticky_arrival_ask_channel(
                self.reservation,
                portal_token=new_access.token,
                session_id=session.pk,
                fallback_channel=GuestMessageChannel.WHATSAPP,
            ),
            GuestMessageChannel.EMAIL,
        )

    def test_g8_failed_new_token_portal_does_not_fallback_to_old_token_ask(self):
        """Token A SUCCESS → regenerate B → portal B FAIL → no ask (no A fallback)."""
        from apps.communications.guest_portal_distribute import (
            resolve_sticky_arrival_ask_channel,
        )
        from apps.reservations.guest_portal_access import regenerate_portal_access

        session = self._complete_wa_session()
        ask_attempts = {"n": 0}

        def fake_wa_portal_then_fail_ask(*args, **kwargs):
            draft = kwargs.get("draft")
            body = kwargs.get("body_text") or ""
            ch = kwargs.get("channel") or GuestMessageChannel.WHATSAPP
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_attempts["n"] += 1
                self._persist(
                    channel=ch,
                    body=body,
                    draft=draft,
                    status=GuestOutboundMessageStatus.FAILED,
                )
                raise RuntimeError("ask boom")
            self._persist(
                channel=ch,
                body=body,
                draft=draft,
                status=GuestOutboundMessageStatus.SENT,
            )
            outbound = MagicMock()
            outbound.status = GuestOutboundMessageStatus.SENT
            outbound.delivery_status = "sent"
            return outbound

        def fake_email_fail(*args, **kwargs):
            draft = kwargs.get("draft")
            body = args[1] if len(args) > 1 else ""
            hint = getattr(draft, "hint", "") if draft is not None else ""
            if hint == HINT_ASK_ARRIVAL_TIME:
                ask_attempts["n"] += 1
                raise AssertionError("ask must not run for failed token B portal")
            self._persist(
                channel=GuestMessageChannel.EMAIL,
                body=body,
                draft=draft,
                status=GuestOutboundMessageStatus.FAILED,
            )
            raise RuntimeError("smtp boom")

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_message",
            side_effect=fake_wa_portal_then_fail_ask,
        ):
            first = send_guest_portal_link_for_session(
                reservation_id=self.reservation.pk,
                session_id=session.pk,
            )
        self.assertEqual(first["status"], "sent")
        self.assertEqual(first.get("arrival_ask_status"), "failed")
        self.assertEqual(ask_attempts["n"], 1)
        token_a = ensure_active_portal_access(self.reservation).token

        _old, new_access = regenerate_portal_access(self.reservation)
        ask_attempts["n"] = 0

        with patch(
            "apps.communications.guest_portal_distribute.send_guest_email_with_timeline_record",
            side_effect=fake_email_fail,
        ):
            second = send_guest_portal_link(
                self.reservation,
                channel=GuestMessageChannel.EMAIL,
                session_id=session.pk,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )

        self.assertEqual(second["status"], "failed")
        self.assertNotIn("arrival_ask_status", second)
        self.assertEqual(ask_attempts["n"], 0)
        # Sticky for B ignores successful A portal.
        self.assertEqual(
            resolve_sticky_arrival_ask_channel(
                self.reservation,
                portal_token=new_access.token,
                session_id=session.pk,
                fallback_channel=GuestMessageChannel.EMAIL,
            ),
            GuestMessageChannel.EMAIL,
        )
        # Old token still resolves to WhatsApp historically — but must not be used.
        self.assertEqual(
            resolve_sticky_arrival_ask_channel(
                self.reservation,
                portal_token=token_a,
                session_id=session.pk,
                fallback_channel=GuestMessageChannel.EMAIL,
            ),
            GuestMessageChannel.WHATSAPP,
        )


class CompleteSessionEnqueuesPortalLinkTests(TestCase):

    def setUp(self):
        today = timezone.localdate()
        self.tenant = Tenant.objects.create(slug="gp-enq", name="GP Enq")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Enq Property",
            slug="enq",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GP-E-1",
            check_in=today,
            check_out=today + timedelta(days=3),
            adults_count=1,
            booker_name="Enqueue Guest",
            booker_email="enq@example.com",
            amount=Decimal("50.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Enq",
            last_name="Guest",
            name="Enq Guest",
            is_primary=True,
            date_of_birth=date(1990, 1, 1),
            nationality="HR",
            sex="female",
            document_number="11223344",
            document_type="identity_card",
            address="Grad Zagreb, Ulica 1",
        )

    @patch(
        "apps.reservations.guest_checkin_tasks.send_guest_portal_link_after_checkin.delay",
    )
    def test_complete_session_enqueues_task_on_commit(self, mock_delay):
        ensured = GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        GuestCheckInOrchestrator.patch_slot(
            ensured.session,
            self.reservation,
            position=1,
            fields={
                "first_name": "Enq",
                "last_name": "Guest",
                "date_of_birth": "1990-01-01",
                "nationality": "HR",
                "sex": "female",
                "document_number": "11223344",
                "document_type": "identity_card",
                "address": "Grad Zagreb, Ulica 1",
            },
        )

        with self.captureOnCommitCallbacks(execute=True):
            completed = GuestCheckInOrchestrator.complete_session(
                ensured.session,
                self.reservation,
            )

        self.assertEqual(completed.session.status, GuestCheckInSessionStatus.COMPLETED)
        mock_delay.assert_called_once_with(self.reservation.pk, completed.session.pk)
