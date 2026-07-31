# ADR 0015 — Unit Photos: canonical model and channel synchronization

## Status

**Accepted** (2026-07-31) — architecture locked (Phase 0 done). Phase A implements models / storage / Mock provider / directory importer (no Channex upload). Phase B: `ChannexPhotoProvider` + outbox worker (hel1 write).

| Phase | Scope | Ships |
|-------|-------|-------|
| **0** | This ADR — SoT, ownership, MediaStorage, validation, state machine, outbox, provider, capabilities, audit | **done** |
| **A** | `UnitPhoto`, `PhotoOutbox`, `UnitPhotoLink`, `MediaStorage` interface, `MockPhotoProvider`, state-machine tests, `import_unit_photos` | **done** |
| **B** | `ChannexPhotoProvider` + worker flush (hel1 write; respect Channex write guard) | **done** (Gate B3 hel1 automated PASS 2026-07-31; Booking visual confirm by ops) |
| **C** | Reception API + UI (capability-gated) | pending |
| **D** | Drift detect → `OUT_OF_SYNC` | pending |
| **E** | Explicit staff resolve (overwrite remote / adopt remote) — only if product wants | pending |

---

## Summary

**Why:** Room listing photos for channels (Channex → Booking.com) must not be edited as SoT in Booking extranet or Channex UI. stay.hr already owns ARI the same way (`RatePlanDay` → outbox → Channex). Photos need the same discipline: one canonical model, async projection, no dual writers.

**How:** stay.hr owns **`UnitPhoto`** (exactly one `Unit`). Bytes live behind **`MediaStorage`**. Sync is always async (`PhotoOutbox` → worker → **`PhotoProvider`**). v1 transport is **`ChannexPhotoProvider`** (Channex Photos API → Booking as Photos provider). Booking.com is never a direct editor.

**Rollback (future code):** feature flag / disable reception photo section; drain outbox; no OTA photo writes when flag off.

---

## Context / Problem statement

- Channex exposes a [Photos Collection](https://docs.channex.io/api-v.1-documentation/photos-collection) (upload + create associated with `property_id` / `room_type_id`). Dynamic URLs are discouraged.
- For Uzorita, Booking.com has approved Channex as **Photos** (and Content) provider — see [channex-booking-editable-settings.md](../../integrations/channex-booking-editable-settings.md).
- stay.hr today has **no** unit listing gallery. Existing media is guest ID / face intake — unrelated.
- Ops temptation: upload photos only in Booking extranet or Channex UI → drift, dual SoT, no reception ownership.
- ARI already uses SoT + outbox (`ChannexAriOutbox`). Promotions architecture is locked in [ADR 0009](0009-channel-promotions.md). Photos must follow that family of decisions.

---

## Decision

### 1. stay.hr is the source of truth

```text
Reception (future)
    → validate
    → MediaStorage
    → UnitPhoto (canonical)
    → PhotoOutbox
    → Celery worker
    → PhotoProvider (ChannexPhotoProvider)
    → Channex Photos API
    → Booking.com
```

- All staff edits land on stay.hr first.
- Channex and Booking.com are **projections**, not editors of record.
- **Rejected:** treating Channex UI or Booking extranet as SoT.
- HTTP handlers never call Channex synchronously for photo mutations.
- **All canonical mutations go through `UnitPhotoService`** (CLI importer, Reception, future API, admin). No direct `UnitPhoto.save()` / `PhotoOutbox.create()` outside that service.

### 2. Ownership — one UnitPhoto, one Unit

- Each **`UnitPhoto`** belongs to **exactly one** **`Unit`** (required FK, tenant-scoped).
- Photos are **not shared** across units in the domain (no M2M, no shared gallery row).
- The same visual used on two rooms ⇒ **two** `UnitPhoto` rows (independent lifecycle, primary, sort, delete, channel sync).
- **`MediaStorage` may deduplicate bytes** (same content checksum → same blob / storage key).
- **Domain never deduplicates photos** — storage sharing is an optimization only; delete/replace on R4 must not affect R3’s `UnitPhoto`.

```text
MediaStorage  = physical object store (may dedupe by checksum)
UnitPhoto     = business entity owned by one Unit
```

**Rejected:** one `UnitPhoto` linked to multiple units; “change once, update all rooms”.

v1 scope = **unit** photos only. Property-level marketing gallery is out of scope (future ADR/entity if needed).

### 3. Pre-canonical validation gate

Nothing becomes a `UnitPhoto` until validation passes:

```text
upload
  → validate (format, dimensions, filesize, mime, optional EXIF cleanup)
  → MediaStorage (persist original)
  → UnitPhoto (+ outbox enqueue)
```

- **Invalid photo never enters the canonical model** — no row, no outbox, no Channex traffic.
- Concrete limits (max MB, min/max pixels) are **config/ops**, not frozen here — only that the gate exists and runs before SoT insert.

### 4. MediaStorage abstraction

ADR does **not** assume local disk.

```text
MediaStorage
  LocalStorage       # dev / current bind-mount
  S3Storage
  CloudflareR2
  MinIO
```

- `UnitPhoto` stores a **storage key / reference**, not a filesystem path.
- Public URL for the provider is produced by storage + immutable URL policy.
- Domain model and Channex sync must not change when the storage backend swaps.

### 5. Immutable URL policy

> A URL must not change its content without changing the URL.

- Cache-safe and CDN-safe.
- Replace always allocates a **new** object key (and new URL) with a new content checksum.
- **Rejected:** overwriting bytes behind an existing public URL.

### 6. Canonical `UnitPhoto` fields (conceptual)

| Field | Role |
|-------|------|
| `unit` | Required FK — sole owner |
| `storage_ref` | MediaStorage key for the **original** |
| `content_checksum` | Hash of **image bytes** (e.g. SHA-256), not metadata-only |
| `is_primary` | Hero / cover; **exactly one** primary per `Unit` |
| `sort_order` | Canonical gallery order; stay.hr always wins |
| `status` | State machine (section 8) |
| soft-delete / timestamps | As other stay.hr models |

**Primary:** changing primary enqueues outbox (`SET_PRIMARY`); provider maps primary to Channex cover (`position = 0`). Invariant enforced in the domain service.

**Sort:** reorder enqueues outbox (`REORDER`); Channex positions are derived from stay.hr order (primary → cover).

**Checksum:** skip provider push when content checksum is unchanged (idempotent no-op). Links/outbox may store last successfully pushed content checksum.

### 7. Image variants

- **Original** bytes in MediaStorage are SoT.
- Derivatives (thumbnail, webp, optimized web size) are **internal implementation**.
- Domain knows **one** `UnitPhoto` per photo; the provider selects the variant URL suitable for Channex.
- **Rejected:** modeling each variant as its own domain photo entity.

### 8. State machine and soft delete

User-visible lifecycle lives on **`UnitPhoto.status`**. Outbox is the async work queue. A single domain/application service owns transitions (same spirit as ADR 0009).

```text
DRAFT
  → UPLOAD_PENDING → SYNCING → ACTIVE

ACTIVE
  → DELETE_PENDING → DELETED

FAILED
OUT_OF_SYNC
```

- Soft delete: no hard delete until remote sync completes (or a documented terminal failure policy).
- Soft-deleted rows retained for audit and retry.
- **No `UPDATE_PENDING` for bytes** — see replace (section 9).

### 9. Replace = delete + upload

Photos are **never edited in place**. User “replace” is a **business operation**:

```text
old photo: ACTIVE → DELETE_PENDING → DELETED
new photo: validate → MediaStorage → UPLOAD_PENDING → SYNCING → ACTIVE
```

Consequences:

- Checksum always matches that row’s bytes.
- Immutable URL preserved (new object → new URL).
- Rollback and audit are two discrete lifecycles.
- No CDN cache poison from overwrite-behind-URL.

**Rejected:** mutating storage bytes of an existing object; reusing the same public URL for different content; treating byte replace as an in-place `UPDATE` of one `UnitPhoto`.

### 10. Outbox and external link

**`PhotoOutbox`** (ARI-like): kinds at least `UPLOAD`, `DELETE`, `REORDER`, `SET_PRIMARY`; status `pending` / `sent` / `failed`; retry; scoped to tenant / property / unit as appropriate. No sync-on-request in the request path.

**`UnitPhotoLink`:**

| Column | Role |
|--------|------|
| `unit_photo_id` | FK to canonical photo |
| `provider` | e.g. `channex` |
| `external_id` | Remote photo id (Channex UUID) — retained after delete |
| content checksum last pushed | Drift / idempotency |
| `last_sync_at` | Last successful sync |
| `deleted_at` / `deleted_checksum` | Soft tombstone after remote DELETE (Phase B audit) |

Normally one active link per photo per provider (`deleted_at IS NULL`). Channex attach target uses the unit’s mapped `room_type_id` (and property id) from existing IntegrationConfig / room mapping — photos do not invent a second mapping table for rooms.

### Phase B implementer notes

- **Idempotent UPLOAD:** skip network when active link checksum matches; else delete+reupload when checksum differs.
- **`LIST` is read-only:** smoke / drift (Phase D) / admin — **not** part of the upload success path (post-upload uses `GET /photos/:id` verify only).
- **Worker:** per-unit `select_for_update`; order DELETE → UPLOAD → coalesced REORDER/SET_PRIMARY; retry classification (429/5xx/timeout vs permanent 4xx); metrics `photo_*_total`.
- **CLI:** `python manage.py flush_photo_outbox --tenant-slug uzorita` (+ `--force-channex-outbound` when WSL write-off).
- **Ops smoke:** [unit-photos-hel1-smoke.md](../../operations/unit-photos-hel1-smoke.md).

### 11. PhotoProvider and capabilities

```text
PhotoProvider
  capabilities() → frozenset[PhotoCapability]
  upload / delete / reorder / set_primary / list   # subset per capabilities

ChannexPhotoProvider   # v1 production path
MockPhotoProvider      # tests
```

**`PhotoCapability`:** `UPLOAD`, `DELETE`, `REORDER`, `SET_PRIMARY`, `LIST`.

- Reception UI and API **hide / reject** operations not in `capabilities()`.
- No byte-level `UPDATE` capability in v1 — replace is delete + upload at the domain layer.
- Domain service is channel-agnostic (`PhotoSyncService` or equivalent) — **not** named as if Channex owned the domain.
- Writes to Channex respect the existing outbound write guard (WSL read-only / hel1 writer).

### 12. Audit

Every domain photo change is audited (same operational expectation as ARI):

- upload, replace, reorder, primary changed, delete

Include actor, unit, photo id, content checksum, and before/after where useful.

---

## Consequences

- Listing photos follow the same SoT → outbox → provider pattern as ARI and ADR 0009 promotions.
- Storage backend can move Local → S3/R2/MinIO without changing `UnitPhoto` or Channex sync semantics.
- Unit ownership avoids cross-room delete/replace bugs; storage dedupe remains allowed.
- Booking extranet and Channex UI must not become parallel editors; drift handling is Phase D/E.
- Concrete validation limits stay configurable; the validation **gate** is mandatory.

---

## Non-goals

- Implementing models, migrations, API, UI, Celery, or providers in this ADR (Phase 0 docs only).
- Property-wide marketing collage / property gallery entity.
- Guest ID / face / document intake media (existing, unrelated).
- Direct Booking.com Photos API as v1 transport.
- Treating remote catalogs as SoT or silent pull-adopt into canonical rows.
- Freezing numeric upload limits (MB / px) inside this ADR.

---

## References

- Channex [Photos Collection](https://docs.channex.io/api-v.1-documentation/photos-collection)
- [channex-booking-editable-settings.md](../../integrations/channex-booking-editable-settings.md) — Photos provider approved for Uzorita
- [ADR 0009 — Channel Promotions](0009-channel-promotions.md) — SoT, outbox, capabilities, drift phases
- `ChannexAriOutbox` — ARI async projection precedent
- Channex outbound write guard / WSL safe mode — single writer for channel mutations
