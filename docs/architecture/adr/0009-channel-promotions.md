# ADR 0009: Channel Promotions — canonical model, provider abstraction, and synchronization

## Status

**Accepted** (2026-07-24) — architecture + Phase 0 closed.

Phase 0 result: Channex support confirmed **no Promotions API** for PMS. Phase B adapter = **`BookingPromotionProvider`** (stay.hr → Booking.com Promotions API). Canonical model / outbox / capabilities / drift policy unchanged. `ChannexPromotionProvider` remains a reserved peer if Channex later ships an API.

Probe: [channex-promotions-api-probe.md](../../operations/channex-promotions-api-probe.md).

| Phase | Scope | Ships |
|-------|-------|-------|
| **0** | Confirm Channex Promotions API — [probe](../../operations/channex-promotions-api-probe.md) | **done** — no API; use Booking Direct |
| **A** | Models, state machine, outbox, `MockPromotionProvider`, unit tests | pending |
| **B** | `BookingPromotionProvider` for `booking_com` / `basic_deal` | pending |
| **C** | Reception API + UI (capability-gated) | pending |
| **D** | Drift detect → `OUT_OF_SYNC` | pending |
| **E** | Explicit staff resolve (push overwrite remote / pull adopt remote) | pending |

---

## Summary

**Why:** Properties need length-of-stay **discounts** (e.g. 3+ nights −10%, 5+ nights −15%) while keeping **1-night** stays bookable at full price. That is a promotion problem, not an ARI min-stay restriction. Binding the domain to “Booking.com Basic Deal” would block Expedia/Airbnb/direct later.

**How:** stay.hr owns a canonical **`ChannelPromotion`**. Sync is always async (outbox → worker → **`PromotionProvider`**). Providers declare **`PromotionCapability`**. Booking.com Basic Deal is the first `promotion_type` on `provider=booking_com`, projected via **`BookingPromotionProvider`** (Phase 0: Channex has no Promotions API).

**Rollback (future code):** feature flag / disable reception promotions section; outbox drain; no OTA auto-writes when flag off.

---

## Context / Problem statement

- Booking.com Extranet **Basic Deal** supports audience, discount %, stay dates, weekdays, min length of stay, rooms/rates, bookable dates/times.
- Channex connection offer lists Promotions (create / update / deactivate / performance) — see [channex-booking-editable-settings.md](../../integrations/channex-booking-editable-settings.md).
- Channex **public PMS API docs** do not expose a Promotions collection. **Phase 0 (2026-07-24):** support confirmed no Promotions API — live probe had `GET /channels/{id}/promotions` → 403 / writes → 404; see [probe](../../operations/channex-promotions-api-probe.md).
- stay.hr today pushes **ARI only** (rates, availability, restrictions) via `ChannexAriOutbox`. Ops note: promotions are Channex UI / extranet — not stay.hr ([channex-uzorita-booking-channel.md](../../integrations/channex-uzorita-booking-channel.md)).
- Channex → Booking **length-of-stay pricing** (different nightly rates by LOS in the rate grid) is **not** available. LOS **percentage deals** via promotions are the supported product path.
- Encoding “3 or 5 night discounts” as `min_stay` restrictions would block shorter stays — rejected by product.

---

## Decision

### 1. stay.hr is the source of truth

```text
Reception UI
  → Reception API
  → ChannelPromotion (canonical)
  → PromotionOutbox
  → worker
  → PromotionProvider
  → OTA / channel manager
```

- All staff edits land on stay.hr first (`DRAFT` / `*_PENDING`).
- Channex and Booking.com are **projections**, not editors of record.
- **Rejected:** treating Channex (or Booking extranet) as SoT and building an “editor for Channex”.

ARI remains SoT for **base** rates and availability. Promotions are **percentage off parent rate**, not a second rate grid.

### 2. Promotion is a Channel feature, not a Booking feature

Canonical entity: **`ChannelPromotion`** (tenant- and property-scoped).

| Field | Role |
|-------|------|
| `provider` | Channel family: `booking_com`, `expedia`, `airbnb`, `direct_website`, … |
| `promotion_type` | Type within that provider: e.g. Booking `basic_deal`, `mobile_rate`, `geo_rate`, `early_booker`, `last_minute` |
| Payload | Type-specific fields (v1 Basic Deal below) |
| `status` | State machine (section 5) |
| `content_checksum` | Stable hash of canonical payload for drift compare |

Do **not** model `kind = basic_deal` alone. Tomorrow:

```text
booking_com / basic_deal | mobile_rate | geo_rate | early_booker | last_minute
expedia    / member_only | package_rate
airbnb     / weekly_discount
direct_website / …
```

**v1 payload** (`provider=booking_com`, `promotion_type=basic_deal`), mapped from Extranet / Booking Promotions API:

| Canonical field | Booking Basic Deal |
|-----------------|--------------------|
| `name` | Internal promotion name |
| `audience` | `public` \| `subscribers` (`target_channel`) |
| `discount_percent` | `discount value` (1–99) |
| `min_stay_nights` | `min_stay_through` |
| `stay_from` / `stay_to` + `weekdays` | `stay_date` + `active_weekdays` |
| `book_from` / `book_to` (optional) | `book_date` |
| `book_time_*` (optional) | `book_time` |
| stay.hr `unit_ids` / rate plan refs | mapped to Booking `rooms` / `parent_rates` at provider boundary |

Product pattern for tiered LOS discounts: **two** promotions (e.g. min 3 / −10% and min 5 / −15%), not one promotion with multiple tiers, and **not** ARI restrictions.

### 3. External mapping table (not JSON blob)

**`ChannelPromotionLink`:**

| Column | Role |
|--------|------|
| `promotion_id` | FK to `ChannelPromotion` |
| `provider` | Transport / account key (`channex`, `booking_direct`, …) — may differ from channel `provider` when Channex projects Booking |
| `external_id` | Remote promotion id |
| `external_version` | Optional remote version / etag |
| `checksum` | Last successfully pushed/observed payload checksum |
| `last_sync_at` | Last successful sync |

Normally one active link per promotion. Table form allows multi-provider ids without stuffing `external_ids` JSON.

### 4. PromotionProvider and capabilities

```text
PromotionProvider
  capabilities() → frozenset[PromotionCapability]
  create / update / delete / list / activate / deactivate   # subset per capabilities

ChannexPromotionProvider      # reserved — Channex has no Promotions API (Phase 0)
BookingPromotionProvider      # v1 production path for booking_com
MockPromotionProvider         # tests
# reserved: ExpediaPromotionProvider, AirbnbPromotionProvider, DirectWebsitePromotionProvider
```

**`PromotionCapability`:** `CREATE`, `UPDATE`, `DELETE`, `LIST`, `ACTIVATE`, `DEACTIVATE`.

- Reception UI and API **hide / reject** operations not in `capabilities()`.
- Tenant config selects which adapter handles `provider=booking_com` (v1 default: **Booking Direct**, not Channex).
- Do **not** name the domain service `ChannexPromotionService` — channel-agnostic application service + pluggable providers.

### 5. State machine

Single writer (domain / application service) owns transitions — same spirit as ADR 0006 import status.

```text
DRAFT
  → SYNC_PENDING → SYNCING → ACTIVE
  → UPDATE_PENDING → SYNCING → ACTIVE
  → (deactivate path) → DEACTIVATED

FAILED          # retry → SYNC_PENDING or UPDATE_PENDING
OUT_OF_SYNC     # drift detected; no automatic push or pull
```

| Status | Meaning |
|--------|---------|
| `DRAFT` | Canonical row exists; not queued for remote |
| `SYNC_PENDING` / `UPDATE_PENDING` | Outbox row enqueued |
| `SYNCING` | Worker holds the outbox item |
| `ACTIVE` | Remote projection matches last successful checksum |
| `FAILED` | Provider error; retryable |
| `DEACTIVATED` | Intentionally inactive on stay.hr and (when capability allows) remote |
| `OUT_OF_SYNC` | Remote differs from canonical; waiting for staff |

HTTP handlers never wait on OTA round-trips; they persist + enqueue and return.

### 6. Outbox (always)

Same pattern as ARI (`ChannexAriOutbox` / `enqueue_outbox_values` / flush task):

```text
save ChannelPromotion
  → ChannelPromotionOutbox (or channel outbox kind=promotion_*)
  → Celery worker
  → PromotionProvider
  → update ChannelPromotionLink + status
```

- UI never calls Booking/Channex synchronously for write.
- Idempotent apply via `external_id` + checksum.
- Failed flush → `FAILED` + `last_error`; does not silently mark `ACTIVE`.

### 7. Conflict / drift policy

If someone edits the deal in Booking Extranet (or Channex UI):

1. LIST / reconcile compares remote snapshot to stay.hr `content_checksum` (and/or link checksum).
2. Diff → status **`OUT_OF_SYNC`** + audit; **do not** auto-overwrite remote; **do not** silent-pull into stay.hr.
3. Phase E: explicit staff actions — **push** (stay.hr wins) or **pull** (adopt remote into canonical).

### 8. Boundaries

| In scope | Out of scope |
|----------|----------------|
| Canonical promotions + provider sync | LOS **pricing** in ARI rate grid |
| % discount deals | Replacing ARI as base price SoT |
| Display of applied promotion from inbound booking meta | Re-pricing stay.hr folio from OTA promotion rules |
| Multi-provider reserved enums | Full Expedia/Airbnb schemas in v1 |

Inbound Channex booking revisions may expose promotion meta — **display / audit only**.

### 9. First implementation target

- **Type:** `provider=booking_com`, `promotion_type=basic_deal`
- **Adapter:** `BookingPromotionProvider` (stay.hr → Booking.com Promotions API)
- **Phase 0 (closed):** Channex support — no Promotions API for PMS. Manual interim: Booking.com Extranet and/or Channex UI. `ChannexPromotionProvider` reserved if they add an API later.
- **Ops note:** Booking Direct needs machine-account / Promotions API credentials separate from Channex ARI key.

---

## Consequences

### Positive

- Domain stays multi-channel; Booking Basic Deal is one type, not the schema.
- Matches ARI async reliability (outbox, no UI blocking on OTA).
- Capability-gated UI avoids fake buttons when a provider cannot LIST/UPDATE.
- Drift policy prevents silent fights with Extranet edits.

### Negative

- Booking Direct needs separate credentials/ops (machine account); Channex ARI path unchanged.
- Two systems can diverge until Phase D/E; staff must resolve `OUT_OF_SYNC`.
- More models than a Booking-only service (`ChannelPromotion` + `Link` + outbox + providers).

---

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Channex or Booking as source of truth | Couples product to one vendor UI; wrong long-term shape |
| `ChannexPromotionService` only | Cannot add Expedia/Airbnb/direct cleanly |
| `external_ids` JSON on the row | Weak multi-link, versioning, and checksum history |
| Sync inside the HTTP request | Same failure mode ARI avoided with outbox |
| Auto-overwrite remote on drift | Dangerous when Extranet is edited intentionally |
| Auto-pull remote into stay.hr | Violates stay.hr SoT without an explicit pull action |
| Encode LOS discounts as `min_stay` restrictions | Blocks 1-night bookings |
| Length-of-stay pricing via Channex ARI | Not offered on Booking through Channex |

---

## References

- Phase 0 probe / ticket: [channex-promotions-api-probe.md](../../operations/channex-promotions-api-probe.md)
- Capabilities snapshot: [channex-booking-editable-settings.md](../../integrations/channex-booking-editable-settings.md)
- Uzorita channel ops: [channex-uzorita-booking-channel.md](../../integrations/channex-uzorita-booking-channel.md)
- ARI outbox pattern: `backend/apps/integrations/models.py` (`ChannexAriOutbox`), `backend/apps/integrations/channex/ari_service.py`
- Booking.com Promotions API (Basic Deal): [Managing promotions](https://developers.booking.com/connectivity/docs/b_xml-promotions)
- Related SoT style: [ADR 0008 Property Settings](0008-property-settings.md); state/audit style: [ADR 0006](0006-booking-payout-financial-source.md)

---

## Implementation log

**Rule:** after each merged phase/PR, append a row — what changed, which decisions are now enforced in code, which tests cover them. A phase is not done until it is logged here.

| Phase | Date | Changes | Tests |
|-------|------|---------|-------|
| **0** | 2026-07-24 | Live probe + Channex support: **no Promotions API**. ADR → **Accepted**. Phase B = `BookingPromotionProvider`. Details: [probe](../../operations/channex-promotions-api-probe.md). | n/a (docs) |
| A–E | — | *pending* | — |
