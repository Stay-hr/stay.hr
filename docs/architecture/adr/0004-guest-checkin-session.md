# ADR 0004: Guest web check-in session

## Status

**Accepted** (2026-07)

**Architecture freeze:** Guest Web Check-in v1

Implemented by:

| PR | Scope |
|----|-------|
| **PR-A** | `GuestCheckInSession`, orchestrator, events, validator, public API, manual wizard, email/WA link distribution |
| **PR-B** | `WEB_GUEST` OCR upload, field confidence, unified foto/manual wizard |
| **PR-C** | Reception progress, regenerate link, version debounce, Channex distribution |
| **PR-D** | `ready_at` analytics, `GuestReminderService`, expiry Celery, KPI metrics |

---

## Summary

Guests complete pre-arrival identity data through a **token-scoped web wizard** (slot-based, autosave) instead of sending documents only in WhatsApp. Reception retains staff control over eVisitor submit. WhatsApp OCR and web check-in are **parallel channels** into the same `Guest` aggregate.

**Public API:** `GET/PATCH/POST` under `/api/v1/public/check-in/{token}/` — see [public-guest-checkin.md](../../api/public-guest-checkin.md).

---

## Context / Problem statement

Hotels must collect guest identity data before arrival (eVisitor, reception workflow). Guests complete identity capture on the **web check-in wizard** (`booking.{tenant}/check-in/{token}`). WhatsApp distributes the link and notifications; it is **not** a document upload channel for guests.

Requirements:

- Secure, shareable link without guest login
- Partial progress preserved (close browser, resume later)
- Reception visibility without requiring guest to click "Finish"
- Same validation rules as eVisitor submit, but **not** coupled to eVisitor mapper during wizard entry
- Multiple intake channels (manual web, web OCR) without duplicating business logic
- One active link per reservation; staff can regenerate

---

## Decision

### `GuestCheckInSession` (not `Reservation.check_in_token`)

| Approach | Why rejected / chosen |
|----------|----------------------|
| `Reservation.check_in_token` | Couples session lifecycle to reservation row; hard to enforce one-active-session, revoke/regenerate, `opens_at`/`expires_at`, `created_from`, `ready_at` analytics |
| **`GuestCheckInSession`** ✅ | Dedicated aggregate: token, window, status transitions, activity audit; max one `active` per reservation (DB constraint) |

Ownership: `apps/reservations/models.py` — `reservations` app.

### `READY` is derived, not persisted

DB statuses: `active` | `completed` | `expired` | `revoked` only.

`effective_status = "ready"` when DB status is `active` **and** all required guest slots pass `GuestValidator`. Reception may submit to eVisitor when derived ready — guest click on "Završi" (`POST /complete/`) is explicit confirmation, not a gate for data availability.

`ready_at` (PR-D) is set **once** on first transition to derived ready (analytics only).

### `GuestCheckInOrchestrator`

Single cross-channel coordinator (`guest_checkin_orchestrator.py`):

- ensure session + URL
- slot PATCH autosave → validator → emit events
- OCR apply callback
- POST complete

**Does not** call side-effect services directly (version bump, audit, reminders). Emits domain events; handlers react.

### `GuestValidator` separate from eVisitor mapper

| Module | Role |
|--------|------|
| `guest_validation.py` → `GuestValidator` | Slot readiness for wizard + reception progress |
| `checkin_readiness.py` | Public DTO (`effective_status`, `can_complete`) |
| `integrations/evisitor/mapper.py` → `build_check_in_payload` | **Staff eVisitor submit only** (`EvisitorReadiness`) |

Wizard must not use eVisitor payload builder as validator — different concerns (UX partial state vs regulatory submit).

### Guest document intake channels (2026-07 amendment)

```text
Manual web PATCH  ──┐
WEB_GUEST OCR     ──┼──► Guest model (per slot) ──► GuestValidator ──► derived ready
WhatsApp (guest)  ──┘   link distribution only — no guest DocumentIntakeJob
                              ▲
                              └── DocumentIntakeJob (WEB_GUEST + legacy WA/Hospira staff paths)
```

- **Manual:** `PATCH .../slots/{position}/`
- **WEB_GUEST:** `POST .../slots/{position}/documents/` → `DocumentIntakeJob(source=web_guest, guest_checkin_slot_position=N)` → slot-forced match → apply → orchestrator events
- **WhatsApp (guest):** Auto check-in and inbound images → **web check-in URL** (`GUEST_CHECKIN_WEB_ONLY=true`). No guest WA OCR batch.
- **WhatsApp / Hospira (staff):** Operator Toni and reception batch OCR unchanged for edge cases.

eVisitor submit remains **staff-only** (reception / Hospira).

### Public DTO API (not `GuestSerializer`)

Public endpoints expose a **minimal, stable DTO** — no internal PK leakage beyond `guest_id` in slot context, no tenant fields, no reception-only data.

Implemented in `apps/api/guest_checkin_views.py`. Authentication: none (`AllowAny`); authorization: unguessable UUID token + session gate.

### Reminders outside domain

Session domain does not know about channels. Automated pre-arrival nudges (`CHECKIN_INFO`, `CHECKIN_LINK`) and day-of WhatsApp welcome (`WELCOME`) are owned by the **Messaging Orchestration Engine** — see [ADR 0010](0010-messaging-orchestration-engine.md).

For live allowlisted scope, legacy `GuestReminderService` is suppressed and CHECKIN_* / WELCOME go through the Messaging Engine only ([ADR 0010](0010-messaging-orchestration-engine.md) Phase 7). Outside that scope (or when flags are off / shadow), legacy may still send pre-arrival nudges (idempotency via `GuestMessageDraft` hint `guest web checkin reminder d{N}`). New automated outbound must not add another specialized sender; use Trigger → MessageDefinition → Dispatcher only.

### Domain events

| Event | Handlers (v1) |
|-------|----------------|
| `GuestSlotReady` | Log |
| `GuestSessionReady` | `touch_reservation_version(CHECKIN)`; set `ready_at` |
| `GuestSessionCompleted` | Version bump; completion metrics |
| `GuestCheckInLinkRegenerated` | Audit log |

---

## Consequences

### Positive

- Clear ownership table (session/validator/orchestrator in `reservations`; public API in `api`; wizard in `web/booking`; automated outbound via Messaging Engine in `communications` — [ADR 0010](0010-messaging-orchestration-engine.md))
- Extensible: new channel = new `created_from` + distribution hook; same orchestrator
- Reception polls `CHECKIN` scope; debounced version on autosave (5 s)
- Analytics funnel: `created_at` → `ready_at` → `completed_at` without extra tables

### Negative

- Token in URL is capability URL — regenerate revokes old; HTTPS required
- Lazy + batch expiry (hourly Celery) — up to 1 h delay after `expires_at` for DB status
- `field_source` per field (OCR vs manual) deferred — see Future work

---

## Non-goals (v1 freeze)

- Guest login / PIN / account registration
- Automatic eVisitor submit
- Replacing reception UI or staff Hospira batch OCR
- Guest WhatsApp document upload (retired 2026-07; web check-in only)
- Offline wizard
- Guest portal for booking changes
- Channex as required MVP channel (implemented PR-C after wizard validation)

---

## Future work

- **`field_source`** on slot fields (`ocr` | `manual`) for OCR quality metrics
- Mobile native client using [public API contract](../../api/public-guest-checkin.md)
- SMS (or other) check-in nudges only via [Messaging Engine](0010-messaging-orchestration-engine.md) provider adapters when a provider exists — not a new `GuestReminderService` channel
- Event bus (Redis/Celery) behind same event contract

---

## References

- Plan: guest web check-in initiative (PR-A … PR-D)
- [Reservation versioning](0001-reservation-event-versioning.md) — `CHECKIN` scope
- [Document intake lifecycle](0002-document-intake-lifecycle-gate.md) — `DocumentIntakeJob` reuse
- [Messaging Orchestration Engine](0010-messaging-orchestration-engine.md) — automated CHECKIN_* / WELCOME distribution; post-v1 = [Rollout & Adoption](../../operations/messaging-orchestration-post-v1.md) (no Phase 9 core)
- Code: `backend/apps/reservations/guest_checkin_*.py`, `backend/apps/api/guest_checkin_views.py`, `web/booking/app/check-in/`
- API: [public-guest-checkin.md](../../api/public-guest-checkin.md)
- Ops: `load_guest_checkin_kpis()`, reception report `/reports/guest-checkin/`
