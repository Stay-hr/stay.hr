# Public guest web check-in API

Token-scoped REST API for the guest check-in wizard. No authentication header — the UUID token **is** the credential.

**Base path:** `/api/v1/public/check-in/{token}/`

**Content-Type:** `application/json` (except document upload: `multipart/form-data`)

**Related:** [ADR 0004 — Guest web check-in session](../architecture/adr/0004-guest-checkin-session.md)

---

## Session gate (all endpoints)

Before any mutation or full read, the backend evaluates session access:

| `gate_status` / `status` | HTTP | Token valid? | Meaning |
|--------------------------|------|--------------|---------|
| `active` | 200 | Yes | Session open; slots incomplete |
| `ready` | 200 | Yes | Derived: all slots ready; guest may complete |
| `not_open_yet` | 403 | Yes | Before `opens_at` — response includes `opens_at` |
| `completed` | 410 | No | Guest submitted check-in |
| `expired` | 410 | No | Past `expires_at` |
| `revoked` | 410 | No | Staff regenerated link |

**Persisted** session status is always one of: `active`, `completed`, `expired`, `revoked`.  
**`ready`** appears only as `effective_status` (never stored on the session row).

Unknown token → **404** (no body contract).

---

## Shared field reference

### Readiness (progress + session)

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Persisted DB session status |
| `effective_status` | string | Business view: `active`, `ready`, or terminal status |
| `required_slots` | int | Expected guest/document slots (`expected_checkin_adults` ?? OTA adults) |
| `ready_slots` | int | Slots passing `GuestValidator` |
| `can_complete` | bool | `true` when `effective_status === "ready"` |
| `waiting_positions` | int[] | Slot positions not yet `ready` (full session only) |
| `ops_version` | int | Optimistic-lock revision (occupancy / commit / complete) |
| `expected_checkin_adults` | int \| null | Check-in occupancy override; `null` = use OTA |
| `adults_count` | int \| null | OTA booked adults (read-only projection) |

### Slot

| Field | Type | Description |
|-------|------|-------------|
| `position` | int | 1-based slot index |
| `guest_id` | int | Internal guest PK (stable for session) |
| `status` | string | `partial` \| `ready` |
| `missing_fields` | string[] | Validator field keys still required |
| `field_errors` | object | Optional map `field → message` (e.g. invalid address format) |
| `field_confidence` | object | Map `field_name → "high"\|"medium"\|"low"` (OCR telemetry; empty if manual only) |
| `guest` | object | Public guest field subset (see PATCH) |

### Confidence values

| Value | Meaning |
|-------|---------|
| `high` | Strong OCR / match signal |
| `medium` | Usable with review |
| `low` | Missing side, weak MRZ, or heuristic fallback |

---

## Endpoints

### `GET /public/check-in/{token}/`

**Purpose:** Initial wizard load — session metadata, all slots, guest field values.

**Response 200:**

```json
{
  "status": "active",
  "effective_status": "active",
  "required_slots": 2,
  "ready_slots": 1,
  "can_complete": false,
  "waiting_positions": [2],
  "booking_code": "BK-12345",
  "property_name": "Hotel Example",
  "check_in": "2026-07-15",
  "check_out": "2026-07-18",
  "opens_at": "2026-07-08T00:00:00+02:00",
  "expires_at": "2026-07-19T23:59:59+02:00",
  "slots": [
    {
      "position": 1,
      "guest_id": 101,
      "status": "ready",
      "missing_fields": [],
      "field_confidence": {
        "first_name": "high",
        "document_number": "high"
      },
      "guest": {
        "first_name": "Ana",
        "last_name": "Anić",
        "date_of_birth": "1990-01-15",
        "nationality": "HR",
        "sex": "female",
        "document_number": "12345678901",
        "document_type": "identity_card",
        "address": "Zagreb, Ulica 1",
        "email": "",
        "phone": ""
      }
    },
    {
      "position": 2,
      "guest_id": 102,
      "status": "partial",
      "missing_fields": ["date_of_birth", "document_number"],
      "field_confidence": {},
      "guest": {
        "first_name": "",
        "last_name": "",
        "date_of_birth": null,
        "nationality": "",
        "sex": "",
        "document_number": "",
        "document_type": "",
        "address": "",
        "email": "",
        "phone": ""
      }
    }
  ]
}
```

**Errors:** 403 `not_open_yet`, 410 terminal statuses, 404 unknown token.

---

### `GET /public/check-in/{token}/progress/`

**Purpose:** Lightweight poll — status and counts only (no guest payloads).

**Response 200:**

```json
{
  "status": "active",
  "effective_status": "ready",
  "required_slots": 2,
  "ready_slots": 2,
  "can_complete": true
}
```

**Errors:** Same gate as session GET.

---

### `PATCH /public/check-in/{token}/occupancy/`

**Purpose:** Set check-in occupancy override (`expected_checkin_adults`) without changing OTA `adults_count`. Used for “I'm traveling alone”.

**Request:**

```json
{ "expected_checkin_adults": 1, "ops_version": 0 }
```

Reset to OTA booked count:

```json
{ "expected_checkin_adults": null, "ops_version": 3 }
```

Rules: integer `1…booked adults`, or `null`. Prunes unfilled secondary guests. Bumps `ops_version`. Emits occupancy audit log.

**Response 200:** Full session payload (same shape as GET session).

**Errors:** 400 `invalid_occupancy`, 409 `session_conflict` (stale/missing `ops_version`).

---

### `PATCH /public/check-in/{token}/slots/{position}/`

**Purpose:** Autosave manual guest fields for one slot. Frontend debounce ~500 ms recommended.

**Request body:** JSON object with any subset of patchable fields:

```json
{
  "first_name": "Ana",
  "last_name": "Anić",
  "date_of_birth": "1990-01-15",
  "nationality": "HR",
  "sex": "female",
  "document_number": "12345678901",
  "document_type": "identity_card",
  "address": "Zagreb, Ulica 1",
  "email": "ana@example.com",
  "phone": "+385911234567"
}
```

Date fields accept ISO `YYYY-MM-DD` strings.

**Patchable fields:** `first_name`, `last_name`, `email`, `phone`, `date_of_birth`, `document_number`, `nationality`, `sex`, `address`, `date_of_issue`, `date_of_expiry`, `issuing_authority`, `personal_id_number`, `document_additional_number`, `additional_personal_id_number`, `document_code`, `document_type`, `document_country`, `document_country_iso2`, `document_country_iso3`, `document_country_numeric`.

**Response 200:**

```json
{
  "status": "active",
  "effective_status": "ready",
  "required_slots": 2,
  "ready_slots": 2,
  "can_complete": true,
  "slot": {
    "position": 1,
    "guest_id": 101,
    "status": "ready",
    "missing_fields": [],
    "field_confidence": {},
    "guest": { }
  }
}
```

**Errors:**

| HTTP | `status` / `detail` | When |
|------|---------------------|------|
| 403 | `not_open_yet` | Before window |
| 404 | `invalid_position` | Position out of range |
| 409 | — | Rare orchestrator conflicts |
| 410 | `completed` / `expired` / `revoked` | Terminal session |

Does **not** require or bump `ops_version` (draft autosave).

---

### `POST /public/check-in/{token}/slots/{position}/commit/`

**Purpose:** Business validation for one slot (`GuestValidator`). Wizard calls this on “Next” after flushing draft PATCH.

**Request:**

```json
{ "ops_version": 0 }
```

**Response 200:** Progress + `slot` (same as PATCH slot response); bumps `ops_version`.

**Errors:**

| HTTP | Code | When |
|------|------|------|
| 409 | `not_ready` | Missing/invalid fields — body includes `missing_fields`, `field_errors` |
| 409 | `session_conflict` | Stale/missing `ops_version` |

---

### `POST /public/check-in/{token}/complete/`

**Purpose:** Guest explicitly confirms check-in. Only when `effective_status === "ready"`.

**Request:**

```json
{ "ops_version": 0 }
```

**Response 200:**

```json
{
  "status": "completed",
  "effective_status": "completed",
  "completed_at": "2026-07-14T18:32:00+02:00"
}
```

**Errors:**

| HTTP | `status` | When |
|------|----------|------|
| 409 | `not_ready` | Slots incomplete — includes `detail` message |
| 410 | terminal | Session no longer active |

---

### `POST /public/check-in/{token}/slots/{position}/documents/`

**Purpose:** Upload ID images for async OCR (`WEB_GUEST` pipeline). Returns job id for polling.

**Request:** `multipart/form-data`

| Part | Required | Notes |
|------|----------|-------|
| `files` | preferred | Multiple files (max 4) |
| `front`, `back`, `file` | fallback | Single-file aliases |

**Response 201:**

```json
{
  "status": "active",
  "effective_status": "active",
  "required_slots": 2,
  "ready_slots": 0,
  "can_complete": false,
  "job_id": 55,
  "status": "queued"
}
```

Note: top-level `status` is session status; job processing status is the duplicate key in current implementation — clients should use `job_id` and poll the job endpoint. *(Job status field: `queued` → `processing` → `done` → applied on poll.)*

**Errors:** 400 `no_files`, `too_many_files`, `file_too_large`; gate errors as above.

---

### `GET /public/check-in/{token}/jobs/{job_id}/`

**Purpose:** Poll OCR job; auto-applies when `done`.

**Response 200 (processing):**

```json
{
  "status": "active",
  "effective_status": "active",
  "required_slots": 2,
  "ready_slots": 0,
  "can_complete": false,
  "job_id": 55,
  "position": 1,
  "error_message": "",
  "processed_at": null,
  "slot": { }
}
```

**Response 200 (done / applied):**

```json
{
  "status": "active",
  "effective_status": "ready",
  "required_slots": 2,
  "ready_slots": 1,
  "can_complete": false,
  "job_id": 55,
  "position": 1,
  "status": "applied",
  "applied": true,
  "guest_preview": {
    "first_name": "Ana",
    "last_name": "Anić",
    "date_of_birth": "1990-01-15",
    "nationality": "HR",
    "sex": "female",
    "document_number": "12345678901",
    "document_type": "identity_card",
    "address": "Zagreb, Ulica 1"
  },
  "field_confidence": {
    "first_name": "high",
    "document_number": "medium"
  },
  "slot": {
    "position": 1,
    "status": "ready",
    "field_confidence": { },
    "guest": { }
  }
}
```

**Errors:** 404 `job_not_found`; gate errors as above.

---

## Client integration notes

1. **Poll strategy:** `GET /progress/` every few seconds during wizard; full `GET /` on mount only.
2. **Complete button:** Show when `can_complete === true`; call `POST /complete/` once.
3. **OCR flow:** Upload → poll `GET /jobs/{id}/` until slot `status` is `ready` or job fails.
4. **403 not_open_yet:** Show `opens_at` to guest; do not retry until window opens.
5. **410:** Show friendly message; guest must request new link from property.
6. **CORS:** Same origin as booking app (`web/booking`); mobile apps call API host directly.

---

## Versioning

**Frozen v1** per ADR 0004. Breaking changes require ADR amendment and versioned path or explicit client negotiation.

Deferred v2: per-field `field_source` (`ocr` \| `manual`) alongside `field_confidence`.
