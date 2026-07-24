# ADR 0007: Guest portal (single URL, contextual cards)

## Status

**Accepted** (PR-A…PR-C + PR-msg, 2026-07)

| PR | Scope |
|----|-------|
| **PR-A** | `GuestPortalAccess`, frozen `GuestPortalContext`, public GET API + gate, booking `/g/{token}` cards (welcome/arrival/parking/wifi/breakfast/contact), Uzorita `guest_info` seed |
| **PR-B** | Key guide card + `self_service_mode` / `is_self_service_active` ✅ |
| **PR-C** | Uzorita Tuesday `schedule`; portal link after check-in on same channel (`created_from`) ✅ |
| **PR-msg** | BOOKING/WhatsApp: two-message portal send (CTA+sign-off, then URL-only) ✅ |
| **PR-D** | ~~Reception GET/PATCH guest-portal editor~~ — **superseded by [ADR 0008](0008-property-settings.md)** (Property Settings; portal is a view) |

> Note: ADR number **0006** is already used by booking payout financial source; guest portal is **0007**.

---

## Summary

Guests always receive the same link `https://booking.{tenant}/g/{token}`. Backend builds a **frozen** `GuestPortalContext` (`sections` + localized `content`); the booking UI only renders what the API returns. Staff edit property-level guest content via **Property Settings** ([ADR 0008](0008-property-settings.md)); the portal is a read-only view of that data. Separated from web check-in (`GuestCheckInSession` on `/check-in/{token}`).

---

## Context / Problem statement

WhatsApp and email currently embed long wifi/entrance/parking blocks. Adding new guest-facing info required new URLs or Meta templates. Reception lacked a structured write API for `Property.guest_info` (Django admin only).

Requirements:

- One stable portal URL per reservation (shareable, no login)
- Token lifecycle independent of check-in wizard (valid through stay)
- UI driven by ordered `sections` — no client-side visibility rules for v1 cards
- Reuse `guest_info` helpers (wifi, parking, entrance, maps, breakfast)

---

## Decision

### `GuestPortalAccess` (not reuse check-in token)

| Approach | Why rejected / chosen |
|----------|----------------------|
| Reuse `GuestCheckInSession.token` | Check-in expires/completes (410); portal must stay open through checkout |
| **`GuestPortalAccess`** ✅ | Dedicated token: `active` \| `revoked`, `opens_at` / `expires_at` (same window helper as check-in), max one active per reservation |

Helpers: `ensure_active_portal_access`, `revoke_portal_access`, `regenerate_portal_access`, `build_guest_portal_url` (`apps/reservations/guest_portal_access.py`).

**Gate:** unknown → 404; before `opens_at` → 403 `not_open_yet`; revoked or past `expires_at` → 410.

### Frozen `GuestPortalContext`

`build_guest_portal_context(access, *, language=None)` resolves language (`?lang=` → `GuestLanguageResolver` PROACTIVE → `en`) and emits ordered sections with localized payloads. HTML/Next does not decide card visibility.

Core sections (PR-A): `welcome`, `arrival`, `parking`, `wifi`, `breakfast`, `contact`.

### Self-service key guide (PR-B / PR-C)

- `Property.self_service_mode`: `off` \| `always` \| `schedule` \| `calendar` + `self_service_config` JSON.
- Helper: `is_self_service_active(property, on_date)` (`apps/properties/self_service.py`).
- Structured `guest_info.guide` (sections + image steps); Uzorita seed merges `uzorita_guide_i18n_extra`.
- Portal adds `key_guide` to `sections` only when active; personalizes `room_code` / `key_label` via `reservation_key_handover_labels`.
- Step images: `backend/assets/guest-portal/uzorita/steps/*.jpg` served under `/api/v1/public/guest-portal/{token}/steps/{index}/`.
- Dry-run: `python manage.py compose_key_handover_guide --reservation-id N`.
- Uzorita seed: `self_service_mode=schedule`, `weekdays=[1]` (Tuesday).

### Portal link after check-in (PR-C + PR-msg)

After `GuestCheckInOrchestrator.complete_session`, Celery `reservations.send_guest_portal_link_after_checkin` ensures `GuestPortalAccess` and sends on the **same channel** as the completed session’s `created_from`:

| `created_from` | Outbound | Shape |
|----------------|----------|-------|
| `channex` | Booking / Channex | **Two messages:** (1) localized CTA + sign-off + footer, (2) URL only (`?lang=`) |
| `whatsapp_autocheckin` | WhatsApp only | Same two-message shape |
| `email` | Email | **One** HTML message (plain CTA + button href; URL not in plain CTA text) |
| `reception_manual` | Email if available, else skip | Same as email |

Compose: `render_guest_portal_link_message` (CTA, no URL) + `render_guest_portal_link_url_only` (URL only). Distribute: `apps/communications/guest_portal_distribute.py`.

Dedup: `GuestMessageDraft.hint = guest_portal_link` (once per reservation; first/CTA draft). Second BOOKING/WA draft uses hint `guest_portal_link url`. If the URL send fails after a successful CTA: `status=partial` with both draft ids. Does not alter Meta welcome templates; does not send WhatsApp when check-in was via Channex/email.

### Public API

`GET /api/v1/public/guest-portal/{token}/` — AllowAny; payload: branding snapshot, language, sections, content, `self_service_active`.

Booking BFF: `/api/g/{token}` → public API; page: `/g/{token}`.

---

## Consequences

- Long wifi/entrance blocks in channel messages can be replaced by a short portal CTA (plus a separate URL message on BOOKING/WhatsApp) without Meta template changes.
- Property Settings ([ADR 0008](0008-property-settings.md)) edits the same `guest_info` / contact / self-service fields without changing the guest URL shape.
- Entrance image served under `/api/v1/public/guest-portal/{token}/entrance/` (BFF `/api/g/{token}/entrance`).
- Key-guide step images under `/steps/{index}/` (BFF `/api/g/{token}/steps/{index}`).

---

## References

- Plan: guest portal one URL / contextual cards
- Property Settings: [0008-property-settings.md](0008-property-settings.md)
- Ops: [guest-portal.md](../../operations/guest-portal.md)
- Check-in ADR: [0004-guest-checkin-session.md](0004-guest-checkin-session.md)
- Seed: `python manage.py seed_uzorita_guest_info`
- Compose dry-run: `python manage.py compose_key_handover_guide --reservation-id N`
