# ADR 0019: Messaging — Conversation Store, Provider Ingest and Delivery

## Status

**Accepted** (2026-08-13) — conversation/inbox architecture locked. Phase A (DB-first GET) is on `main`. Phase B (membership, media-at-ingest, ingest idempotency/version, observability) is implemented. Phase C client GET contract is closed: Reception web and Hospira ([Stay-hr/hospira_flutter](https://github.com/Stay-hr/hospira_flutter) #1, `4273268`) use DB-only GET (`sync=0` or omitted). Backend still ignores `sync` so older Hospira builds remain compatible. Phase D **D1 schema** and **D2 dual-write** are on `main`. **D3** backfill is on `main` (`backfill_canonical_guest_messages`). **D4** adds a tenant read-flag (`read_canonical_at`) so GET/inbox can read visible `GuestMessage` with the same JSON shape plus additive `canonical_id`; default GET stays on raw `timeline_for_reservation()` until `--enable`.

Canonical identity: `GuestMessage` is the logical UI row; `GuestMessageSource` holds 1..N external/raw identities. `provider_message_id` is nullable; missing provider IDs must not be fabricated.

This is a **module evolution ADR**, not yet a platform standard. [ADR README](README.md) still requires production proof before promoting patterns to platform-wide standards. Phase 0 documents what already runs in production; Phase A is the first slice that must prove the read/write split.

**Not ADR 0010.** [ADR 0010](0010-messaging-orchestration-engine.md) is the **Messaging Orchestration Engine** (Trigger → MessageDefinition → Dispatcher for automated CHECKIN_* / WELCOME). This ADR is the **conversation/inbox infrastructure**: where a message lives, how it is ingested, how it is read, how duplicates are merged, and how delivery status is tracked. The engine **may consume** this layer; it does **not** own the inbox.

---

## Summary

**Why:** Reception already stores guest messages in Django, but opening a conversation still treats Channex (and sometimes IMAP) as the read model. That couples UI latency to third-party APIs, hides ingest failures behind “refresh”, and would let a later `GuestMessage` model grow in the wrong direction.

**How:** Stay.hr’s database is the source of truth for the conversation UI. Providers (Channex, WABA, IMAP/SMTP, later others) are ingest and delivery adapters. User-facing GET has no provider I/O. Canonical conversation storage is phased so we can ship a DB-first read path now without a big-bang refactor.

Normative pipeline:

```text
Channex / WABA / IMAP / SMTP / (future providers)
        ↓
webhook / poll / send adapter   ← ingest & delivery, never UI read
        ↓
Stay.hr database (raw ingest + conversation read model)
        ↓
Conversation / Timeline / Inbox API
        ↓
Reception web / Hospira Flutter
```

Normative rule (required):

> **Providers are not the read model for UI.** Opening, listing, or polling a conversation MUST read Stay.hr’s database. Provider fetch MUST NOT block opening a conversation.

This is the inbound/read counterpart of [ADR 0016](0016-external-integration-pattern.md) (local process is source of truth; remote I/O is best-effort and not a hidden side effect of a user GET).

---

## Context / Problem statement

Guest messaging on stay.hr already spans three channels, four persistence tables, webhooks, Celery polls, duplicate merging, reservation versioning, FCM, and two clients (reception web + Hospira Flutter). The pain is not “we have no store”. The pain is that **the store is not treated as the read model**.

### What already exists (Phase 0 — current production)

| Layer | Today |
|-------|--------|
| Channex / Booking.com | `ChannexMessage`; webhook `message`; API `list_booking_messages` / send; Celery reconcile (15 min, Uzorita) = Eligible ∩ (A∪B∪C∪D) per §9 |
| WhatsApp Cloud API | `WhatsAppMessage`; inbound webhook; `WhatsAppInboundRouting` for unlinked messages; media on `media_file` |
| Email | `GuestInboundMessage` via IMAP poll (Celery 120 s); outbound via tenant SMTP into `GuestOutboundMessage` |
| Outbound audit | `GuestOutboundMessage` for compose/send (email / booking / whatsapp) |
| Thread UX state | `GuestMessageThreadState` (needs-reply dismissed, conversation language) |
| Translation cache | `GuestMessageTranslation` keyed by source table + row id |
| Automated outbound | ADR 0010 engine (`MessageDispatch` outbox) — **not** the inbox |
| Timeline API | `GET …/reservations/{id}/messages/` unions the four tables in Python, merges echoes, returns a list |
| Inbox API | `GET …/message-threads/` aggregates last timeline entry per reservation |
| Realtime signal | `touch_reservation_version(scope=messages)` + poll `sync-versions`; FCM `guest.message.received` |
| Clients | `GuestMessagesPanel` (web), Hospira Flutter (`docs/development/guest-messages-flutter.md`) |

Synthetic timeline ids (`WA_ID_OFFSET`, `CHANNEX_ID_OFFSET`, `INBOUND_ID_OFFSET`) exist because there is no canonical message PK. Duplicate merge windows (outbound 180 s, inbound 900 s) exist because Booking.com mail relay often lags Channex for the same guest text.

### What is wrong

1. **GET has side effects.** Default `?sync=auto` (and client `?sync=1` on every panel mount) calls Channex `list_booking_messages` and, on `sync=1`, polls the whole tenant IMAP inbox **before** returning the timeline. The user waits on Booking.com via Channex even though rows are already in Postgres.
2. **Inbox can amplify that.** `message-threads/?sync=1` may iterate Channex-linked reservations and pull each one.
3. **ADR 0001 encoded the wrong mount behaviour for messages.** Version-change refresh is already `sync=0` (correct). Mount / tab-return / 5-minute interval still force `sync=1` ([ADR 0001](0001-reservation-event-versioning.md) decision §4). This ADR **supersedes that for the messages panel**.
4. **Four tables + Python merge is an implicit read model.** A future `GuestMessage` built without this ADR would either wrap Channex as SoT or invent a fifth parallel store.
5. **Completeness is confused with freshness.** Missed webhooks are recovered by making the receptionist wait on a provider, not by a bounded reconciliation job.

### Requirements

- Opening a conversation is a local read (fast, works if Channex/Meta/IMAP are down).
- Ingest is push-first (webhook), pull is catch-up (Celery / explicit reconcile).
- Idempotent upsert per provider identity.
- One conversation UX across channels, without treating Booking.com as the product.
- Automated campaigns (ADR 0010) send through delivery adapters and appear in the same conversation store.
- Phased delivery: no requirement to land a canonical table before the read-path split.

---

## Decision

### 1. Split three concerns (permanent)

| Concern | Owns | Does not own |
|---------|------|----------------|
| **Conversation store** (this ADR) | Persistence, identity, dedupe, timeline/inbox read model, delivery status as local sync state | Campaign scheduling, template plans, quiet hours |
| **Provider ingest & delivery** | Webhooks, polls, send adapters, attachment download | UI fetch-on-open |
| **Orchestration engine** ([ADR 0010](0010-messaging-orchestration-engine.md)) | When/why an automated message is planned (CHECKIN_*, WELCOME, future Adoption flows) | Inbox, timeline merge, receptionist compose |

Manual compose, AI reply, invoice mail, and engine dispatches are **producers** into the conversation store. They must not each grow a private timeline.

### 2. Source of truth

- **UI-visible conversation** = Stay.hr database.
- **Provider systems** = external copies used to send and to ingest. Divergence is expected and must be visible (delivery state, ingest lag), not hidden by refetch-on-open.
- **Raw ingest tables** (`ChannexMessage`, `WhatsAppMessage`, IMAP rows) remain the provider-shaped audit/payload store. After Phase D they are **not** queried by the UI.

Reservation-scoped conversation is the v1 identity:

> One **Conversation** per `(tenant_id, reservation_id)`.

This matches today’s timeline and inbox. It is **not** a WhatsApp `wa_id` thread, not a Channex `message_thread_id`, and not a guest-person graph.

Unrouted inbound (WhatsApp without reservation, Channex message before booking link) lives in **ingest/routing queues**, not in a fake conversation. Routing (`WhatsAppInboundRouting`, `relink_unlinked_channex_messages`) is part of ingest (Phase B), not a UI blocking fetch.

**Cross-tenant WABA:** same class of invariant as document intake. The Cloud API account may live on the platform/WABA tenant; the conversation row MUST use **`reservation.tenant_id`**. Pipeline code must not use the WABA tenant as the conversation tenant.

### 3. GET has no provider side effects

Applies to:

- `GET …/reservations/{id}/messages/`
- `GET …/message-threads/`
- any future conversation/inbox read, including Flutter

Normative behaviour:

| Query | Meaning after Phase A |
|-------|------------------------|
| omitted / `sync=0` | **Default.** Read DB only. Never call Channex/IMAP/WABA. |
| `sync=auto` | Deprecated alias of `sync=0`. Must not pull “if empty”. Empty means ingest has not landed yet — show empty, do not block on a provider. |
| `sync=1` | **Not allowed to block the response.** Either ignored on GET, or accepted only as a hint to enqueue background reconcile. The JSON body is still the current DB snapshot. |

Catch-up is an **explicit write-side operation**: Celery task, management command, or `POST …/messages/reconcile/` (ops / pull-to-refresh that does not delay first paint). Retry of provider I/O is never a hidden side effect of opening the panel ([ADR 0016](0016-external-integration-pattern.md) forbids silent re-sync on every refresh).

`channels` (what can be sent right now: SMTP configured, Channex link, WA session window) is **capability**, not history. It may read local config and reservation fields. It must not list remote message histories.

### 4. Ingest contract (all providers)

Every inbound path:

1. Authenticate webhook / poll as today.
2. Resolve reservation (or leave in routing queue).
3. **Idempotent upsert** on provider identity (raw table today; `GuestMessageSource` in Phase D — see §7).
4. Persist body + media locally (download attachments at ingest; UI serves Stay.hr media URLs).
5. If a **new UI-visible** row appeared: `touch_reservation_version(scope=messages)` and FCM `guest.message.received` when the product already does so.
6. Do **not** bump version on duplicate upsert, attaching another source to an existing logical message, delivery receipts, or read receipts ([reservation-versioning.md](../reservation-versioning.md)).

Provider identity keys (Phase B freeze on **raw** tables; Phase D copies the same keys onto `GuestMessageSource`, **not** onto `GuestMessage`):

| Provider | Idempotency key |
|----------|-----------------|
| Channex | `channex_message_id` (already unique) |
| WhatsApp | `wamid` (already unique) |
| Email / IMAP | `(tenant_id, Message-ID)` **where present**. If the message has no `Message-ID`, do not invent one — see §7 (`provider_message_id` nullable). |

Echo merge (same guest text arriving via Channex **and** `@guest.booking.com` minutes later) is a **read-model heuristic until Phase D**. Do not delete raw rows to “fix” duplicates. Phase D attaches a second `GuestMessageSource` to the **same** `GuestMessage` — it does not create a second canonical row and then `merged_into` it.

Ordering key for display: provider timestamp when trustworthy (`received_at` / Channex created), else ingest `created_at`. Document the Booking.com mail lag; do not “fix” it by live-pulling Channex from GET.

### 5. Outbound / delivery (align with ADR 0016)

Sending a message is a local business action:

```text
Compose/send (receptionist or engine)
        ↓
Local commit (conversation row + outbound audit)   ← source of truth that we intended to send
        ↓
Provider adapter (SMTP / Channex / WABA)
        ↓
Explicit delivery_status (queued / sent / failed / …)
        ↓
Idempotent retry (explicit), not “open the chat again”
```

- Remote timeout must not roll back the local conversation row.
- `delivery_status` is **sync state**, distinct from “the message exists in the thread”.
- Engine `MessageDispatch` remains the outbox for **automated** flows; when Adoption migrates compose, it still writes through this store so the timeline does not fork.
- Phase E deepens retry/receipts; Phase A must not invent a second outbound table.

### 6. Realtime is “event → DB read”

After ingest or send:

1. Writer touches `ReservationVersion` scope `messages` (and FCM where applicable).
2. Client on version change / push fetches **`sync=0`** (or equivalent DB read).
3. Client never translates “new message” into “call Channex”.

Transport (poll vs SSE vs Redis) stays [ADR 0001](0001-reservation-event-versioning.md) / [ADR 0005](0005-gunicorn-sse-worker-evolution.md). This ADR only forbids provider refetch on the messages scope.

Inbox lists are the same rule: version or FCM may refetch **threads from DB**, not N Channex GETs.

### 7. Canonical model (Phase D — identity locked now, tables later)

One **logical** message can be observed through several providers. That is already true in production (Channex row + Booking.com mail relay minutes later). Putting a single `(provider, provider_message_id)` on `GuestMessage` would force either a Channex-shaped canonical row or a second `GuestMessage` plus `merged_into_id` for every echo — a migration we would then have to undo.

Locked shape:

```text
Conversation
    ↓
GuestMessage              ← one logical message (UI row)
    ↓
GuestMessageSource        ← 1..N external / raw identities
```

Example (today’s echo, stored explicitly):

```text
GuestMessage #481
  conversation = reservation 798
  direction    = inbound
  channel      = booking
  body         = "Dolazimo oko 18h"
  occurred_at  = 2026-08-13T16:02:00Z

GuestMessageSource
  message              = #481
  provider             = channex
  provider_message_id  = "ch_123"
  raw                  = ChannexMessage #921

GuestMessageSource
  message              = #481
  provider             = imap
  provider_message_id  = "<booking-xyz@mail.booking.com>"
  raw                  = GuestInboundMessage #442

# IMAP without Message-ID — no fabricated external id:
GuestMessageSource
  message              = #482
  provider             = imap
  provider_message_id  = NULL
  raw                  = GuestInboundMessage #123
```

#### `Conversation`

v1: 1:1 with reservation (`tenant_id`, `reservation_id`). Unchanged from §2.

#### `GuestMessage` (logical)

UI and translations key off this row. It is **not** provider-shaped.

| Field | Role |
|-------|------|
| `conversation` | Parent |
| `direction` | inbound / outbound |
| `channel` | Product channel: `booking` / `email` / `whatsapp` (what the receptionist sees) |
| `body`, `media` | Canonical display payload (best available; may be filled from the first source and enriched) |
| `occurred_at` | Display timestamp (earliest trustworthy provider time) |
| `delivery_status` | Local sync state for outbound (ADR 0016); not a Channex/Meta enum dumped onto the row |

**Not on `GuestMessage`:** `provider`, `provider_message_id`, raw payload, Channex booking UUID, `wamid`, IMAP `Message-ID`. Those belong on `GuestMessageSource`.

`channel` ≠ `provider`. A Booking.com guest line is `channel=booking` even when one source is Channex and another is IMAP. WhatsApp stays `channel=whatsapp` with provider `waba` (and later maybe SMB echo as a second source).

#### `GuestMessageSource` (external identity)

Also acceptable name: `GuestMessageExternalRef`. This ADR uses **`GuestMessageSource`**.

| Field | Role |
|-------|------|
| `message` | FK → `GuestMessage` |
| `provider` | `channex` / `waba` / `imap` / `smtp` / `stay_outbound` / … |
| `provider_message_id` | **Nullable.** Provider’s stable external id when the provider actually supplies one. Never a synthetic stand-in. |
| `raw` | **Required.** Pointer to the provider-shaped row (`ChannexMessage`, `WhatsAppMessage`, `GuestInboundMessage`, `GuestOutboundMessage`, …) |

Constraints:

```text
If the provider supplies a stable external id:
    UNIQUE (tenant_id, provider, provider_message_id)
    WHERE provider_message_id IS NOT NULL

If it does not:
    the raw row identity is the ingest identity;
    canonical dedupe/merge MUST NOT depend on a fabricated external id.
```

Concrete mapping:

| Provider | `provider_message_id` |
|----------|------------------------|
| Channex | `channex_message_id` |
| WhatsApp | `wamid` |
| IMAP with `Message-ID` | that `Message-ID` |
| IMAP without `Message-ID` | `NULL`; ingest identity = `GuestInboundMessage` PK (raw pointer) |

Do **not** use empty string, hash-of-body, or `local-inbound:…` as `provider_message_id`. Empty string would collide under a naive unique constraint; a hash would merge unrelated mails.

- A webhook/poll for an already-seen **non-null** `(tenant_id, provider, provider_message_id)` attaches nothing new and creates no second `GuestMessage`.
- Re-processing the same **raw PK** (including IMAP with null Message-ID) is idempotent via the required raw pointer (**unique** per raw row). A *new* raw row without Message-ID may still heuristic-merge onto an existing `GuestMessage` (body/window) or become a new logical message — same as today.
- **Raw rows are never deleted** to express a duplicate. The extra observation is another `Source`.
- Prefer **explicit nullable FKs** to known raw tables (or an equivalent constrained pointer) over an unconstrained GenericFK as the only integrity. Adding a future provider may add a nullable FK; it must not add columns onto `GuestMessage`.

#### Ingest / dual-write algorithm (Phase D)

For each raw upsert:

1. Write/update the **raw** table (unchanged provider payload).
2. If `provider_message_id` is present: lookup `GuestMessageSource` by `(tenant_id, provider, provider_message_id)`. Else lookup by **raw pointer**.
3. **Hit** → existing logical message. Stop. No `touch`.
4. **Miss** → in the same `Conversation`, heuristic-match an existing `GuestMessage` (same `direction`, body/media compatible, time window — today’s 180 s outbound / 900 s inbound). On match: insert `GuestMessageSource` only. No second canonical row. No `touch` unless the logical display payload actually changed (e.g. attachment arrived only on the second source).
5. **No match** → insert `GuestMessage` + first `GuestMessageSource`, then `touch` + FCM as today.

Race where two logical rows are created before the echo is recognized: a **rare reconcile** may move `Source` rows onto a survivor and hide the loser. That is an exception job, **not** the primary identity (`merged_into_id` is not the model).

Backfill: create one `GuestMessage` per current timeline-merged group, then one `GuestMessageSource` per raw row in that group — do not 1:1 map each `ChannexMessage` to its own `GuestMessage`.

#### What the UI reads

After cutover, timeline/inbox read **`GuestMessage` only**. Sources and raw tables are ingest/audit/debug.

`GuestMessageThreadState` / `GuestMessageTranslation` re-key onto `GuestMessage.id`.

**Forbidden in Phase D:**

- dropping raw tables in the same PR as the cutover
- pointing the UI at Channex or at `GuestMessageSource` as the timeline
- a single `(provider, provider_message_id)` on `GuestMessage` as the only external identity
- fabricating `provider_message_id` when the provider omitted one (use `NULL` + required raw pointer)
- a parallel `PaymentVersion`-style poll for messages

Until Phase D, `timeline_for_reservation()` **is** the read model (Python union + heuristic merge). New channels must land in that union (or wait for Phase D), not a second panel-specific fetch. Heuristic merge today is the stand-in for “attach another Source”.

API stability: Phase A–C keep the current timeline JSON shape (additive fields allowed). Phase D may introduce stable `GuestMessage.id` and must version or dual-write until Flutter/web stop using offset synthetic ids.

### 8. What this ADR does not decide yet (non-goals)

- Guest-person conversation across reservations (CRM thread)
- SMS / Viber / Instagram as products (channel adapters can be added later under the same ingest/read split)
- Replacing Meta, Channex, or IMAP
- Kafka / extra event bus for messages
- Full-text search, cursor pagination (allowed later; full timeline GET is OK through Phase C)
- Migrating manual compose onto ADR 0010 (Adoption track; this store must exist either way)
- Staff/operator WhatsApp (Toni / Hospira batch) as the same conversation as the guest — keep operator tooling separate unless a later ADR merges them
- Message retention / GDPR erasure procedure (must be designed before treating the store as a long-term archive; flag as follow-up, do not block Phase A)

### 9. Phased delivery

```text
Phase 0  Document current state and ownership          ← this ADR
Phase A  DB-first read path; GET never waits on provider
Phase B  Reliable background ingest (webhook + reconcile + idempotency)
Phase C  Realtime UI = event → DB refresh only
Phase D  Canonical Conversation + GuestMessage + GuestMessageSource
Phase E  Outbound delivery status / retry (ADR 0016 applied to send)
```

Phases are sequential for **architecture**, not a single release train. Phase A is on `main`. Phase B must not become a performance/read-model PR (`GuestMessage` stays Phase D; inbox latency stays Phase D). GET stays DB-only.

#### Phase 0 — Ownership (this document)

| Owner | Code / docs |
|-------|-------------|
| Conversation read (today) | `apps.communications.guest_message_timeline`, `message_threads_service` |
| HTTP | `apps.api.reception_guest_messages_views`, `reception_message_threads_views` |
| Channex ingest/send | `apps.integrations.channex.message_service`, webhook, `message_tasks` |
| WABA ingest | `apps.integrations.whatsapp.*`, `WhatsAppMessage` |
| IMAP ingest | `apps.communications.guest_email_ingest`, `email_ingest_tasks` |
| Send / compose | `guest_message_send`, `guest_compose` |
| Engine | `apps.communications.messaging` (ADR 0010) |
| UI | `web/reception/app/_components/GuestMessagesPanel.tsx`, Hospira Flutter |
| Ops | `docs/operations/guest-messages-channels.md` |

#### Phase A — DB-first read (PR1)

Must:

- Default timeline and inbox GET to DB-only (`sync=0`).
- First paint of a conversation never awaits Channex or IMAP.
- Version-change and FCM handlers already using `sync=0` stay that way.
- Mount / visibility / interval **must not** call `sync=1` as the opening request. Optional: after first paint, enqueue background reconcile without blocking.
- If `sync=1` remains on GET for Flutter compatibility, the handler still **returns DB first**; provider work is skipped or async. Prefer documenting “use POST reconcile + GET 0” and updating Flutter in the same or immediately following PR.
- Tests: timeline/inbox GET with `sync=1`/`auto` must not require a live/mock Channex round-trip to succeed.
- Update ADR 0001 consumers, Flutter runbook, and `guest-messages-channels.md` so “Refresh = Channex pull” is no longer the documented happy path.

Must not:

- Introduce `GuestMessage` / `GuestMessageSource`
- Change timeline JSON breaking clients
- Remove webhooks or Celery IMAP/Channex jobs
- Make empty timeline call Channex “just in case”

#### Phase B — Background ingest

Goal: what Phase A already reads quickly from the DB is **reliably ingested**. Phase B is write-side catch-up, not a read-model change.

- Treat Channex webhook `message` as the primary inbound path; keep Messages & Reviews + `send_data=true` as ops invariant.
- IMAP poll stays Celery (not a side effect of opening one reservation).
- WABA webhook remains primary; no Meta history fetch from UI.
- Dedupe/idempotency tests per identity key; relink unlinked Channex/WA as jobs.
- Observability: last webhook / last poll / ingest lag per channel on `GET …/system/status/` top-level ``conversation`` (`metrics_scope=cluster`). Do **not** put inbox lag on ADR 0010 ``messaging`` engine health.
- Media: download at ingest; missing attachment is ingest debt, not a reason to refetch the thread from the provider on GET:

```text
provider → ingest → media_file → GET

media_file missing
        ↓
404 / local state + observability
        ✕
no Channex fetch from GET
```

##### Conversation ingest observability (locked)

Dedicated top-level ``conversation`` on `GET /api/v1/reception/system/status/` (`reception:read`). `metrics_scope=cluster` — shared across Gunicorn workers and Celery via Redis, unlike per-worker SSE counters. Do **not** add these fields to ADR 0010 ``messaging``.

| Channel | `last_webhook_at` | `last_poll_at` |
|---------|-------------------|----------------|
| **channex** | `process_channex_message_webhook` (including duplicate upserts) | Celery reconcile cycle and CLI `sync_channex_booking_messages` |
| **whatsapp** | `record_inbound_whatsapp_message` (including duplicate `wamid`; not missing `wamid`) | none (webhook-only) |
| **email** | none (IMAP poll) | after successful IMAP `INBOX` select (empty UID search still counts; connect failure does not) |

`ingest_lag_seconds` = whole seconds since `max(last_webhook_at, last_poll_at)` for that channel; `null` when neither stamp exists.

##### Automatic Channex reconcile membership (locked)

Reconcile is a **bounded catch-up for missed webhooks**, not a UI read path and not “every inbox thread”. Celery membership is implemented (`channex_reconcile_membership_qs`). Channex media is downloaded at ingest (`ensure_channex_message_media`); GET must not fetch. Ingest stamps (`last_webhook_at` / `last_poll_at` / `ingest_lag_seconds`) live on `GET …/system/status/` ``conversation``.

**Eligible** (all required):

- `import_source = channex`
- `_reservation_can_sync_messages` is true
- `status` not in `canceled`, `no_show`, `refused`, `pending`

**Membership** = **A ∪ B ∪ C ∪ D**:

| Leg | Rule |
|-----|------|
| **A** | `expected`, `check_in ∈ [today, today+7d]` — first Booking.com message before a local `ChannexMessage` exists |
| **B** | `checked_in` (all in-house) |
| **C** | `checked_out`, `check_out ∈ [today−1d, today]` |
| **D** | a `ChannexMessage` exists with `created_at ≥ now−7d` (`created_at` = ingest time on the raw row, not OTA timestamp) |

```text
Eligible:
- import_source = channex
- _reservation_can_sync_messages = true
- status not in canceled, no_show, refused, pending

Membership = A ∪ B ∪ C ∪ D

A: expected, check_in ∈ [today, today+7d]
B: checked_in
C: checked_out, check_out ∈ [today−1d, today]
D: exists ChannexMessage.created_at ≥ now−7d

Cadence:
- every 15 min
- one reconcile cycle for the whole set
- dedupe reservations before the provider call
```

**D must not re-admit a reservation the status filter excludes.** A recent `ChannexMessage` on a `canceled` / `no_show` / `refused` / `pending` reservation does **not** put that reservation in the set. Apply Eligible (including the status filter) **after** the union, not only on A/B/C.

**Explicit exclusions:**

```text
- far-out expected > +7d without recent activity (leg D)
- checked_out older than 1 day without recent activity (leg D)
- canceled / no_show / refused / pending
- inbox thread membership is not a criterion
```

**Cadence:** keep **15 minutes**, one cycle for the whole set. Dedupe reservation PKs before `list_booking_messages`. Do not split cadence in the first Phase B PR unless load requires it.

**Observation threshold (not a hard-coded business rule):** if a cycle exceeds **~80** reservations, inspect D / windows / cadence before widening the set. Do not grow membership to “all expected” or “all inbox threads” without amending this ADR.

**Not in this set:** one-off CLI / future `POST …/messages/reconcile/` for a single reservation remains the escape hatch (incident, far-out first message, canceled thread). Those paths are write-side and must not be invoked from GET.

Baseline (Uzorita, 2026-08-13): current task ≈ 5 reservations/cycle; A∪B∪C∪D ≈ 35. Leg D is required: without it, active threads outside the calendar window (already known to be corresponding) would not be healed.

#### Phase C — Realtime UI

Done for the client GET contract (not Phase D models, not a new SSE bus):

```text
event / mount / refresh / FCM / send
        ↓
DB-only GET (`sync=0` or omitted)
        ↓
UI

never:
client event → sync=1/auto → provider reconcile
```

- Opening and staying on a thread: DB reads only.
- Web: `GuestMessagesPanel` always `GET …/messages/?sync=0` (mount, version bump, refresh, post-send). Dead `shouldRunFullSync` is removed.
- Hospira: inbox and thread GETs default `sync=0`; `refreshWithSync` / `forceSync` no longer mean provider reconcile ([hospira_flutter #1](https://github.com/Stay-hr/hospira_flutter/pull/1)).
- Pull-to-refresh shows local data immediately; catch-up is ingest + version bump, not GET.
- Inbox refresh is the same.
- Do not add a second SSE channel for message bodies; reuse reservation versioning.
- Reviews stay on their own `sync=auto` contract (out of this ADR).

#### Phase D — Canonical store

**D1 (schema only):** tables, CHECK/UNIQUE constraints, tenant-match triggers, and identity tests. No dual-write, no backfill, no GET/timeline change.

**D2 (dual-write):** `record_canonical_source` after every raw upsert (create and existing) in the same `transaction.atomic()` as the raw write. Four funnels: Channex upsert/relink, IMAP ingest, routed WhatsApp inbound, outbound `GuestOutboundMessage` / WhatsApp. Unrouted WA stays in routing queues. GET/timeline unchanged.

**D3 (historical backfill):** `python manage.py backfill_canonical_guest_messages --tenant-slug` writes one `GuestMessage` per today's timeline merge group and one `GuestMessageSource` per source member (display members plus suppressed WA/outbound mirrors). Cutoff PKs and `completed_at` live on `CanonicalConversationBackfill`. GET/timeline unchanged until D4 `--enable`.

**D4 (read cutover):** `set_canonical_guest_message_read --tenant-slug` (`--parity` / `--validate` / `--enable` / `--disable`). Synthetic `id` stays; additive `canonical_id` is `GuestMessage.pk`. Hidden WA/outbound mirrors never become the primary display source. `--enable` requires D3 complete, live validation, and JSON parity. Deploy does not auto-enable.

- Add `Conversation` + `GuestMessage` + `GuestMessageSource` as specified in §7; dual-write from ingest/send; backfill by **merged groups**, not 1:1 raw→canonical.
- Switch `timeline_for_reservation` to `GuestMessage`; then stop UI reads of raw tables.
- Heuristic echo merge becomes “attach `GuestMessageSource`”, not a second canonical row.
- Replace synthetic ids behind a compatible API.
- Pagination/search may land here, not before.

#### Phase E — Delivery

- Uniform `delivery_status` / attempts on canonical messages (and/or reuse engine `MessageDeliveryAttempt` for automated sends).
- Explicit retry API; receipts (WABA delivered/read) update sync state without timeline duplication and without `touch` unless the UI shows those states.
- Manual compose remains allowed to stay on `GuestOutboundMessage` until Adoption; do not create a third outbox.

### 10. PR review checklist (messaging)

For any PR that touches guest messages, inbox, Channex/WABA/IMAP ingest, or reception/Flutter chat:

1. Does a **user-facing GET** still avoid provider I/O?
2. Is Stay.hr DB the **read model** for that change?
3. Is provider work on **webhook, Celery, send adapter, or explicit POST reconcile**?
4. Is upsert **idempotent** on the provider identity key (raw today; `GuestMessageSource` in Phase D — never a second logical `GuestMessage` for the same source)?
5. On new UI-visible rows, is **`touch_reservation_version(messages)`** called — and **not** called on duplicates/receipts?
6. If adding a channel or table, does it flow through the **timeline/inbox read model** (or Phase D canonical), not a one-off fetch?
7. Does it avoid expanding ADR 0010 with inbox concepts (and avoid expanding this ADR with campaign DSL)?
8. If it changes automatic Channex pull: is membership still **A ∪ B ∪ C ∪ D** with the Eligible status filter applied **after** D (canceled + recent `ChannexMessage` stays out)?

If any answer is **no**: justified exception in the PR, or it is debt that must not merge.

### 11. Forbidden without amending this ADR

- Defaulting timeline/inbox GET back to Channex/IMAP pull
- New client that lists Booking.com / Meta threads as SoT
- Parallel `MessageVersion` / `COUNT`/`MAX` polling ([ADR 0001](0001-reservation-event-versioning.md))
- New automated sender that bypasses ADR 0010 **and** bypasses this store
- Canonical `GuestMessage` that is Channex-shaped (Booking-only columns as the product schema)
- `(provider, provider_message_id)` as the **only** external identity **on** `GuestMessage` (those columns belong on `GuestMessageSource`; one logical message has 1..N sources)
- Inventing a `provider_message_id` for IMAP (or any provider) that did not supply one
- Widening automatic Channex reconcile beyond **A ∪ B ∪ C ∪ D** (all future expected, all inbox threads, GET-triggered pull)
- Channex attachment fetch from a user-facing GET when `media_file` is missing

---

## Consequences

### Positive

- Opening messages is a Postgres read; provider outages do not blank the thread.
- Ingest quality becomes measurable (webhook vs reconcile) instead of “hit Refresh”.
- Phase D has a decided identity: `Conversation` per reservation, `GuestMessage` as the logical UI row, `GuestMessageSource` for 1..N provider/raw identities — so `GuestMessage` cannot become Channex-shaped or a fifth ad-hoc table.
- ADR 0010 stays a campaign engine; this ADR stays an inbox. Each can evolve without rewriting the other.
- Matches ADR 0016: no silent remote I/O on read; outbound commit-then-sync.

### Negative

- First paint can be **briefly stale** if a webhook is delayed (mitigate with Phase B reconcile + Phase C events — not with blocking GET).
- Flutter and ops docs that treat `sync=1` as the way to “really load Booking.com” must change in Phase A.
- Empty thread for a brand-new Channex booking is a valid state until ingest runs.
- Phase D is still a real migration (dual-write, synthetic ids). This ADR only prevents doing it blindly.

### Follow-ups (do not block Phase A)

- Retention / guest data erasure for stored bodies and media
- Person-scoped conversation (multi-stay)
- Whether operator WhatsApp shares the guest conversation
- Cursor pagination once timelines are large
- Inbox-level version signal (today versioning is per reservation)

---

## Alternatives considered

| Alternative | Why not chosen |
|-------------|----------------|
| Small `sync=0` fix with no ADR | High risk that Phase D `GuestMessage` treats Channex as SoT or adds another table beside the four we already merge |
| Canonical `GuestMessage` in the first PR | Large migration, synthetic ids, Flutter contract, dual-write — blocks the latency win |
| `(provider, provider_message_id)` on `GuestMessage` plus `merged_into_id` | Encodes one provider as the row; Booking.com Channex+IMAP echoes become two canonical messages and a later migration. Identity belongs on `GuestMessageSource` (1..N). |
| Fabricated `provider_message_id` when the provider omitted one (hash, empty string, `local-…`) | Collides or false-merges; contradicts existing email uniqueness “where present”. Use `NULL` + required raw pointer. |
| Keep `sync=1` on mount “until webhooks are perfect” | Makes GET the reconciliation mechanism forever; never proves ingest SLA |
| Conversation keyed by WhatsApp `wa_id` or Channex thread | Breaks email-only and Booking.com-only stays; routing queues exist for a reason |
| Fold inbox into ADR 0010 | Engine is outbound automation; inbox/read/dedupe/realtime are a different domain |
| UI reads providers directly (BFF to Channex/Meta) | Couples UX to third-party latency and auth; duplicates what we already persist |
| Remote success required before showing outbound | Violates ADR 0016; receptionist blocked when Channex returns 403 |
| Calendar-only reconcile (A∪B∪C, drop D) | Leaves a hole for threads already known to be active outside the stay window; D is bounded catch-up for missed webhooks, not inbox membership |
| Automatic pull of every inbox thread / all future expected | Unbounded Channex API load; inbox is a read-model union (Phase D), not ingest eligibility |

---

## Implementation slices

Phase A is done (DB-first GET). Automatic Channex reconcile membership (**A ∪ B ∪ C ∪ D**) is implemented in Celery.

Phase B is done:

1. ~~Implement automatic Channex reconcile as **A ∪ B ∪ C ∪ D**~~ (done).
2. ~~Media-at-ingest; GET must not fetch Channex attachments~~ (done).
3. ~~Idempotency / version tests~~ (done).
4. ~~Observability (`last_webhook_at` / `last_poll_at` / lag) on ``conversation``, not ADR 0010 ``messaging``~~ (done).

Phase C client GET contract is done (web already DB-only; Hospira #1 merged). Do not start Phase D models in a Phase C docs/cleanup PR.

---

## References

- Ops today: [guest-messages-channels.md](../../operations/guest-messages-channels.md)
- Flutter contract: [guest-messages-flutter.md](../../development/guest-messages-flutter.md)
- Versioning: [reservation-versioning.md](../reservation-versioning.md), [ADR 0001](0001-reservation-event-versioning.md) (messages mount `sync=1` **superseded** by this ADR §3 / Phase A)
- External sync pattern: [ADR 0016](0016-external-integration-pattern.md)
- Orchestration (not this): [ADR 0010](0010-messaging-orchestration-engine.md), [messaging-orchestration-post-v1.md](../../operations/messaging-orchestration-post-v1.md)
- SSE transport: [ADR 0005](0005-gunicorn-sse-worker-evolution.md)
- Check-in session vs channels: [ADR 0004](0004-guest-checkin-session.md)
- Timeline merge / views: `backend/apps/communications/guest_message_timeline.py`, `backend/apps/api/reception_guest_messages_views.py`
- Ingest: `backend/apps/integrations/channex/message_service.py`, `backend/apps/communications/guest_email_ingest.py`
