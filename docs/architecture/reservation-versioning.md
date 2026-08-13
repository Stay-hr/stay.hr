# Reservation versioning

**Reservation Versioning Infrastructure** je platformski mehanizam za sinkronizaciju UI stanja rezervacije. Moduli ne komuniciraju izravno s frontendom, nego signaliziraju promjene kroz `ReservationVersion`, dok frontend dohvaća podatke tek kada se verzija promijeni.

Event-driven monotonic counters per reservation and **scope** power cheap auto-refresh for reception panels (guest messages today; payments, documents, check-in, housekeeping later).

**Related:** [ADR 0001 — Reservation event versioning](adr/0001-reservation-event-versioning.md) · [Architecture review (2026-07)](reservation-versioning-architecture-review.md) · [AGENTS.md — Reservation versioning](../../AGENTS.md#reservation-versioning)

---

## Overview

`ReservationVersion` stores a single integer per `(reservation, scope)`. It increases only when writers call `touch_reservation_version()` — never by direct `version += 1` elsewhere.

The reception frontend polls `sync-versions` with a `scope` query parameter. When `versions[scope]` changes, the panel fetches fresh data in the background. When nothing changed, the server returns **304 Not Modified** (ETag match), avoiding payload serialization.

This lives in **`backend/apps/reservations/`** (platform infra), not in the communications module, so internal module moves do not break the API.

---

## Scopes

Scopes are **stable domain names** exposed in the HTTP API:

| Scope | Enum | Purpose (v1+) |
|-------|------|----------------|
| `messages` | `ReservationVersionScope.MESSAGES` | Guest message timeline |
| `payments` | `ReservationVersionScope.PAYMENTS` | Payment panel (future) |
| `documents` | `ReservationVersionScope.DOCUMENTS` | Guest documents (future) |
| `checkin` | `ReservationVersionScope.CHECKIN` | Check-in status (future) |
| `housekeeping` | `ReservationVersionScope.HOUSEKEEPING` | Housekeeping (future) |

### Naming rules

- Use **business domain** names (`messages`, `payments`), not module or package names (`guest_messages`, `communications`, `billing_module`).
- Existing scope names **must not be renamed** without a new API version or route.
- New scopes may be added without versioning the HTTP API — clients that do not poll them are unaffected.

### Adding a new scope

1. Add a value to `ReservationVersionScope` in `backend/apps/reservations/models.py` and migrate.
2. Call `touch_reservation_version(reservation_id, scope, reason="…")` from every writer that changes **UI-visible** state for that domain.
3. Wire the frontend panel with `useTimelineVersionPoll({ scope: "your_scope", … })`.
4. Add writer-hook regression tests (touch called / not called per scenario).

---

## When to touch

Bump the version when the user would see different content in the panel.

### Touch (UI-visible)

| Source | `reason` (examples) |
|--------|---------------------|
| WhatsApp inbound (timeline row) | `whatsapp_inbound` |
| WhatsApp outbound sent / handoff | `whatsapp_outbound`, `whatsapp_handoff` |
| WhatsApp autocheckin reply (timeline) | `whatsapp_autocheckin_reply` |
| Email inbound | `email_inbound` |
| Email outbound (sent) | `email_outbound` |
| Channex new message (new visible row) | `channex_message` |

### Do not touch (internal / invisible)

- WhatsApp `delivered` / `read` status webhooks
- `pending_send` retry metadata
- Internal logs, debug fields, cache rebuilds
- IMAP poll, Channex sync deduplication (duplicate upsert)
- Email outbound failure (no timeline row shown)

---

## touch vs publish

### `touch_reservation_version()`

The only allowed bump API. It:

1. Optimistic `UPDATE … SET version = F("version") + 1` (with `get_or_create` + retry on first-bump race)
2. Structured log: `touch reservation_version reservation=… scope=… reason=… old=… new=…`
3. Calls `publish_reservation_version_changed(reservation_id, scope, new_version)`

Must stay **lightweight**: no IMAP, Channex fetch, cache rebuild, heavy SQL, or network calls.

Location: `backend/apps/reservations/reservation_version.py`

### `publish_reservation_version_changed()`

Event dispatcher stub in v1 (debug log only). Future extensions (SSE, WebSocket, Redis, AI notifications) modify **only** this function — `touch` and frontend hooks stay unchanged.

---

## sync-versions API

**Route:** `GET /api/v1/reception/sync-versions/`  
**Reception proxy:** `/api/stay/reception/sync-versions/`

### Query parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `reservation_id` | Yes when `scope` is set | Reservation PK (tenant-scoped) |
| `scope` | Optional | `messages`, `payments`, …, or `all` |
| `year` | Required for legacy full payload | Calendar year for statistics hash (default: current year) |

When `scope` is present, the response is **only** `{ "versions": { … } }` (slim poll). When omitted, the legacy payload includes `reservations`, `rooms`, `statistics`, optional `reservation_detail`, and `versions` when `reservation_id` is set.

### Response shape

**Single scope** (requested key always present, including `0`):

```json
{ "versions": { "messages": 17 } }
```

**`scope=all`** (only scopes that exist in DB):

```json
{
  "versions": {
    "messages": 17,
    "payments": 8
  }
}
```

Never return `{ "versions": {} }` for a single-scope request.

### ETag / 304

- ETag = SHA-256 of canonical JSON (`sort_keys=True`) of the **entire** response payload, weak format `W/"…"`.
- Client sends `If-None-Match`; matching ETag → **304** with empty body.
- Scoped and full payloads produce **different** ETags.

Implementation: `backend/apps/reservations/sync_versions.py`, `backend/apps/api/reception_views.py` (`ReceptionSyncVersionsView`).

---

## API Compatibility

Rules that protect frontend clients from accidental breaking changes:

| Rule | Detail |
|------|--------|
| New scope | May be added without HTTP API versioning |
| Rename | Existing scopes **must not** be renamed |
| Semantics | Meaning of an existing scope **must not** change (what triggers a bump) |
| Response shape | `{ "versions": { … } }` is stable — key `versions`, values are integers |
| Single-scope | Requested scope key **always** present (including `0`) |
| ETag / 304 | Hash of full payload; `If-None-Match` → 304 without body |

Breaking changes require a new route, explicit API version, or a revised ADR — not a silent refactor.

---

## Performance Goals

Code review criteria for this infrastructure:

| Component | Target |
|-----------|--------|
| `touch_reservation_version()` | **< 5 ms** median/p95 per call (local DB, no network) |
| `sync-versions?reservation_id=X&scope=messages` | **One SELECT** on `(reservation_id, scope)` — no N+1 |
| Poll without change | **304** + `If-None-Match` — no full body serialization |
| Poll query | **No** `COUNT` / `MAX` / `GROUP BY` on each scoped poll |
| `scope=all` | One SELECT for all version rows for `reservation_id` |
| `publish_*` (v1) | No-op or debug log — must not push touch above latency target |

Regression tests: `backend/apps/reservations/tests/test_reservation_version.py`, `test_sync_versions.py`, `backend/apps/api/tests/test_reception_api.py`.

Frontend: poll ~5 s while tab visible; full `/messages` fetch only on version change (`sync=0`, background).

---

## SSE transport (v2)

Push is implemented in `publish_reservation_version_changed()` via `ReservationVersionEventBus` (`reservation_version_event_bus.py`). Default backend is **in-process** fan-out; set `RESERVATION_VERSION_EVENT_BUS=redis` for multi-worker / Celery delivery (ADR 0005 Phase 2a). Redis replaces distribution only — SSE lifecycle instrumentation stays on.

**Endpoint:** `GET /api/v1/reception/reservation-versions/stream/?reservation_id=&scope=`

- `Content-Type: text/event-stream`
- Events: `connected` (initial version), `reservation_version_changed` (`{ reservation_id, scope, version }`)
- Heartbeat comments every ~25 s

### Phased roadmap

| Phase | What changes | Frontend contract |
|-------|--------------|-------------------|
| **Phase 1** (now) | Gunicorn sync workers + in-process fan-out; poll fallback | Unchanged |
| **Phase 1 operational tuning** | `GUNICORN_WORKERS=12`; `GuestMessagesPanel` → `transport: "poll"` (1 SSE per reservation detail) | Unchanged |
| **Phase 2a** | `ReservationVersionEventBus` → Redis (`stay:v1:reservation_version:{tenant_slug}`); flag `RESERVATION_VERSION_EVENT_BUS` | Unchanged |
| **Phase 2b** | Dedicated Uvicorn SSE service; Gunicorn REST only | Unchanged |
| **Phase 2c** (optional) | Full Django ASGI | Only if independently justified |

**Phase 1 operational tuning note:** `GuestMessagesPanel` uses `transport: "poll"` to reduce concurrent SSE streams per reservation detail tab. This is an operational adjustment within Phase 1 — not a new architecture phase. Timeline version watch may still use SSE.

**Observability (Phase 1):**

- `GET /api/v1/reception/system/status/` — **reception:read** (operational; not public). `schema_version: 4`, Gunicorn config, worker PID/uptime, SSE counters (`active`, `peak`, `connections_opened_total`, `connections_closed_total`, `average_duration_seconds`), EventBus raw fields, thin `database` probe, Messaging Engine inventory (`messaging` — ADR 0010), conversation ingest stamps (`conversation` — ADR 0019; `metrics_scope=cluster`), and derived `components.{event_bus,sse,database}` (`healthy`|`warning`|`critical` + `reason`). SSE/`event_bus` remain per worker.
- Structured logs: `sse_stream_opened`, `sse_stream_closed` with `duration_s`, `worker_pid`
- Gunicorn env: `GUNICORN_*` via `scripts/run-gunicorn.sh`; monitoring checklist: [gunicorn-sse-monitoring.md](../operations/gunicorn-sse-monitoring.md)
- Incident postmortem: [2026-07-08 SSE worker exhaustion](../operations/incidents/2026-07-08-sse-worker-exhaustion.md)

**Frontend:** `useReservationVersionWatch({ transport: "sse" })` — default for panels. Falls back to `useTimelineVersionPoll` if SSE is unavailable. Panels use only `onVersionChange` → `loadTimeline()`.

Poll + ETag/304 remain supported and unchanged.

**Future (via `publish_*` only — no changes to `touch_reservation_version()` or panel contracts):**

- Phase 2a: `ReservationVersionEventBus` with Redis pub/sub envelope (`producer`, `event_id`, monotonic `version`)
- Phase 2b: dedicated Uvicorn SSE service (REST never routed to Uvicorn)
- Phase 2c (optional): full Django ASGI — not required for SSE scaling

---

## Adding a new module (example: documents)

1. **Model scope** — add `DOCUMENTS` to `ReservationVersionScope`.
2. **Writers** — after creating/updating/deleting a user-visible document row:

   ```python
   touch_reservation_version(
       reservation_id,
       ReservationVersionScope.DOCUMENTS,
       reason="document_uploaded",
   )
   ```

3. **Frontend panel** — reuse the hook:

   ```typescript
   useTimelineVersionPoll({
     reservationId,
     scope: "documents",
     onVersionChange: () => loadDocuments({ sync: 0, background: true }),
   });
   ```

4. **Tests** — writer hooks + `assertNumQueries(1)` for scoped poll.

---

## Governance

**Novi modul koji uvodi stanje rezervacije** mora koristiti `ReservationVersion` (`touch_reservation_version`, `sync-versions` + `scope`) i **ne smije** uvoditi vlastiti mehanizam pollinga ili verzioniranja bez odobrene arhitekturne odluke (ADR).

**Zabranjeno bez ADR-a:** paralelni modeli (`PaymentVersion`, `HousekeepingVersion`, …), vlastiti poll hookovi, `COUNT`/`MAX` na svakom zahtjevu umjesto event-driven bumpa.

Cross-reference: [AGENTS.md — Reservation versioning](../../AGENTS.md#reservation-versioning).

---

## Frontend integration (guest messages, v1)

| File | Role |
|------|------|
| `web/reception/lib/useTimelineVersionPoll.ts` | Poll `scope`, ETag, pause when tab hidden |
| `web/reception/lib/shouldRunFullSync.ts` | Retired (ADR 0019): always false; do not `sync=1` on GET |
| `web/reception/app/_components/GuestMessagesPanel.tsx` | Mount `sync=0`; version change → `sync=0` background |

Opening the messages panel is a DB read. Provider ingest is webhook + Celery, not mount/interval GET.

---

## Manual verification checklist

Use reservation `/reservations/973` (or any reservation with WhatsApp linked):

1. Mount → one `sync=0` timeline fetch (DB-only; [ADR 0019](adr/0019-messaging-conversation-store.md)).
2. WhatsApp inbound → new message appears within ~5 s without F5.
3. `delivered` webhook → no timeline refresh / no version bump.
4. Tab hidden → no `sync-versions` polls (Network tab quiet).
5. Tab visible, no changes → poll returns **304**.

Automated coverage: `test_reservation_version.py`, `test_reservation_version_writer_hooks.py`, `test_sync_versions.py`, `test_reception_api.py` (scoped ETag/304).

---

## Key files

| Layer | Path |
|-------|------|
| Model | `backend/apps/reservations/models.py` |
| Touch / publish | `backend/apps/reservations/reservation_version.py` |
| sync-versions helpers | `backend/apps/reservations/sync_versions.py` |
| HTTP view | `backend/apps/api/reception_views.py` |
| Writers | `integrations/whatsapp/tasks.py`, `communications/guest_message_*.py`, `integrations/channex/message_service.py` |
| Frontend | `web/reception/lib/useTimelineVersionPoll.ts`, `GuestMessagesPanel.tsx` |
| Tests | `backend/apps/reservations/tests/test_reservation_version*.py`, `test_sync_versions.py` |
