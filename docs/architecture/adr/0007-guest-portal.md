# ADR 0007: Guest portal (single URL, contextual cards)

## Status

**Accepted** (PR-A…PR-C + PR-msg, 2026-07; post-checkin guardrails G1–G8, 2026-08)

| PR | Scope |
|----|-------|
| **PR-A** | `GuestPortalAccess`, frozen `GuestPortalContext`, public GET API + gate, booking `/g/{token}` cards (welcome/arrival/parking/wifi/breakfast/contact), Uzorita `guest_info` seed |
| **PR-B** | Key guide card + `self_service_mode` / `is_self_service_active` ✅ |
| **PR-C** | Uzorita Tuesday `schedule`; portal link after check-in on same channel (`created_from`) ✅ |
| **PR-msg** | BOOKING/WhatsApp portal send shape (now: **one** CTA+URL message; superseded two-message split) ✅ |
| **Post-checkin G1–G8** | `last_distributed_from`, arrival-ask after portal success, success-only dedup, atomic claims, sticky/latest channel, current-token scope, plain `/g/` in `body_text` ✅ |
| **PR-D** | ~~Reception GET/PATCH guest-portal editor~~ — **superseded by [ADR 0008](0008-property-settings.md)** (Property Settings; portal is a view) |

> Note: ADR number **0006** is already used by booking payout financial source; guest portal is **0007**.

---

## Summary

Guests always receive the same link `https://booking.{tenant}/g/{token}`. Backend builds a **frozen** `GuestPortalContext` (`sections` + localized `content`); the booking UI only renders what the API returns. Staff edit property-level guest content via **Property Settings** ([ADR 0008](0008-property-settings.md)); the portal is a read-only view of that data. Separated from web check-in (`GuestCheckInSession` on `/check-in/{token}`).

After web check-in completes, the platform sends a short portal CTA (plain text includes `/g/{token}?lang=…`) on the check-in channel, then an arrival-time ask under guardrails **G1–G8**.

---

## Context / Problem statement

WhatsApp and email currently embed long wifi/entrance/parking blocks. Adding new guest-facing info required new URLs or Meta templates. Reception lacked a structured write API for `Property.guest_info` (Django admin only).

Requirements:

- One stable portal URL per reservation (shareable, no login)
- Token lifecycle independent of check-in wizard (valid through stay)
- UI driven by ordered `sections` — no client-side visibility rules for v1 cards
- Reuse `guest_info` helpers (wifi, parking, entrance, maps, breakfast)
- Post-checkin: visible portal link in reception timeline; arrival ask once; correct channel after Autocheck-in reuse

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

### Portal link after check-in (PR-C + post-checkin)

After `GuestCheckInOrchestrator.complete_session`, Celery `reservations.send_guest_portal_link_after_checkin` ensures `GuestPortalAccess` and sends on the check-in completion channel:

| `created_from` | Outbound | Shape |
|----------------|----------|-------|
| `channex` | Booking / Channex | **One** message: CTA + `/g/{token}?lang=` + sign-off |
| `whatsapp_autocheckin` | WhatsApp | Same single-message shape |
| `email` | Email | Plain `body_text` includes URL; HTML keeps CTA button |
| `reception_manual` | Email if available, else skip | Same as email |

Compose: `render_guest_portal_link_message` (CTA + URL + sign-off). Distribute: `apps/communications/guest_portal_distribute.py`.

Channel routing prefers successful check-in link distribution via `GuestCheckInSession.last_distributed_from` (G1: stamped **only** after provider-accepted check-in link send; `created_from` stays immutable for analytics).

### Post-checkin guardrails (G1–G8)

| # | Rule |
|---|------|
| **G1** | `last_distributed_from` only after successful check-in link send |
| **G2** | Arrival ask only after portal `status=sent` (no ask on fail/partial) |
| **G3** | Portal dedup: successful outbound for session + **current** token + channel; failed ≠ block; `allow_resend` bypass |
| **G4** | Ask dedup success-only, channel-agnostic; no `allow_resend` on ask |
| **G5** | Atomic DB claim (`PostCheckinSendClaim`): `pending`/`sent` block, `failed` reclaimable; provider I/O outside txn |
| **G6** | Sticky ask channel = successful portal outbound (not live `last_distributed_from`) |
| **G7** | Latest **successful** portal for session+token owns orphan ask; failed resend does not steal |
| **G8** | Scope to current portal token after regenerate; no fallback to old token |

Claim keys: `guest_portal:{session_id}:{token}:{channel}`, `arrival_ask:{session_id}`.

Reception timeline shows plain `body_text` (so `/g/` is visible) and a **failed** badge when outbound `status=failed`.

Does not alter Meta welcome templates; does not send WhatsApp when check-in was via Channex/email (unless that was the successful distribution channel).

### Public API

`GET /api/v1/public/guest-portal/{token}/` — AllowAny; payload: branding snapshot, language, sections, content, `self_service_active`.

Booking BFF: `/api/g/{token}` → public API; page: `/g/{token}`.

---

## Consequences

- Long wifi/entrance blocks in channel messages are replaced by a short portal CTA **with URL in plain text** (timeline-visible) without Meta template changes.
- Property Settings ([ADR 0008](0008-property-settings.md)) edits the same `guest_info` / contact / self-service fields without changing the guest URL shape.
- Entrance image served under `/api/v1/public/guest-portal/{token}/entrance/` (BFF `/api/g/{token}/entrance`).
- Key-guide step images under `/steps/{index}/` (BFF `/api/g/{token}/steps/{index}`).
- Concurrent complete/retry races are gated by Postgres claim rows, not Redis.

---

## References

- Plan: guest portal one URL / contextual cards; post-checkin portal flow (G1–G8)
- Property Settings: [0008-property-settings.md](0008-property-settings.md)
- Ops: [guest-portal.md](../../operations/guest-portal.md)
- Check-in ADR: [0004-guest-checkin-session.md](0004-guest-checkin-session.md)
- Seed: `python manage.py seed_uzorita_guest_info`
- Compose dry-run: `python manage.py compose_key_handover_guide --reservation-id N`
