# Guest messages — tri kanala (Mail, Channex, WhatsApp)

Operativni runbook za slanje i primanje poruka gostima iz stay.hr recepcije (web + Flutter).

---

## Kanali

| Kanal | Outbound | Inbound u stay.hr | Rezervacije |
|-------|----------|-------------------|-------------|
| **Mail** | Tenant SMTP (`room_reservations@uzorita.hr`) | Da (IMAP poll, Booking.com `@guest.booking.com`) | Sve s emailom |
| **Channex** | Channex Messages API → B.com Poruke | Da (webhook `message`) | `import_source=channex` |
| **WhatsApp** | Handoff (`wa.me`) | Da (WhatsApp webhook) | S telefonom |

**Vlastita booking platforma** (stay.hr web booking, `source=api`): nema guest app — koristi **Mail** (+ WhatsApp). Inbound odgovori gosta na mail s `@guest.booking.com` ulaze u chat timeline (IMAP poll svake 2 min).

**B.com PDF import** (`import_source=booking_pdf`): Mail na `@guest.booking.com` + WhatsApp; Channex API **nije** dostupan (nema Channex linka).

---

## Channex setup (jednokratno)

1. Channex UI → Property → Apps → **Messages & Reviews** — instalirano
2. Webhook (isti URL kao bookingi):
   ```text
   https://api.stay.hr/api/v1/integrations/channex/webhook/?provider=stay&env=staging
   ```
   Header: `X-Stay-Channex-Webhook: <secret iz IntegrationConfig>`
3. Dodati event **`message`** s **`send_data=true`**

---

## API (recepcija)

| Metoda | Ruta |
|--------|------|
| GET | `/api/v1/reception/message-threads/` — inbox (kartice po rezervaciji) |
| GET | `/api/v1/reception/reservations/{id}/messages/` — unified chat timeline |
| POST | `/api/v1/reception/reservations/{id}/messages/compose/` |
| POST | `/api/v1/reception/reservations/{id}/messages/send/` — body: `{ draft_id, channel, body_text }` |
| GET | `/api/v1/reception/reservations/{id}/channex-messages/` — samo Channex (legacy; DB-only GET) |

Timeline and inbox GET are **DB-only** ([ADR 0019](../architecture/adr/0019-messaging-conversation-store.md) Phase A/C). Reception web and Hospira send `?sync=0` (or omit). Query `?sync=1` / `auto` is ignored and does **not** pull Channex or poll IMAP — older Hospira builds stay compatible. Ingest is webhook + Celery (`poll_guest_email_inbox`, `sync_channex_messages_for_upcoming_checkins`).

Ingest freshness: `GET /api/v1/reception/system/status/` → ``conversation.channels.{channex,whatsapp,email}`` (`last_webhook_at`, `last_poll_at`, `ingest_lag_seconds`). This block is **cluster** scope (Redis, shared by Gunicorn + Celery), not the per-worker SSE counters. It is **not** ADR 0010 ``messaging`` engine health.

Automatic Channex reconcile (every 15 min, Uzorita) is **A ∪ B ∪ C ∪ D** after an Eligible filter (`import_source=channex`, can sync, status not canceled/no_show/refused/pending). D (recent `ChannexMessage`) cannot re-admit excluded statuses. CLI `sync_channex_booking_messages` remains the one-off backfill / incident escape hatch — not GET.

Channex attachments are downloaded at ingest (webhook + reconcile). `GET …/channex-messages/{id}/media/` serves local `media_file` only; missing file is **404**, never a live Channex fetch.

`channel` u send: `email` | `booking` | `whatsapp`

---

## Reception web

Property-wide inbox: **Poruke** → `/messages` (`GET …/message-threads/?sync=0`). Kartica otvara `/reservations/{id}#messages` u novom tabu. Compose ostaje na detalju rezervacije.

Detalj rezervacije → sekcija **Poruke gostu** (`#messages`, [`GuestMessagesPanel.tsx`](../../web/reception/app/_components/GuestMessagesPanel.tsx)):

- Chat timeline
- Generiraj (check-in / odgovor / prilagođeno)
- Odabir kanala: Mail / WhatsApp / Channex (samo dostupni); **Mail** je zadani kad je dostupan

---

## Guest e-mail replay (historical messages, no automation)

Use when the IMAP poll was not running and the missed messages must be recorded **without** re-running the live
automation (LLM, auto-replies, push, language/reservation updates). Code: `apps/communications/guest_email_replay.py`.
Always run one-offs through `django-run` (see AGENTS.md).

```bash
# 1. Dry-run (default). Reads the mailbox read-only, writes NOTHING. Output = uid, date, outcome only.
docker compose --profile test-run run --rm django-run python manage.py replay_guest_email \
  --tenant uzorita --from-uid 1122 --to-uid 1128 --expect-uidvalidity <UIDVALIDITY>

# 2. Apply (only after reviewing the dry-run). One DB transaction: messages + cursor move, or nothing.
docker compose --profile test-run run --rm django-run python manage.py replay_guest_email \
  --tenant uzorita --from-uid 1122 --to-uid 1128 --apply --expect-cursor 1121 --expect-uidvalidity <UIDVALIDITY>
```

| Guard | Behaviour |
|---|---|
| Mailbox access | `EXAMINE` (read-only), bodies via `BODY.PEEK[]`: no flags change, nothing is moved/deleted. Content stays in memory, never logged, never printed. |
| Default mode | Dry-run. `--apply` is refused without `--expect-cursor`. |
| Cursor | `--expect-cursor` must equal `guest_imap_last_uid` (re-checked under a row lock); `--from-uid` must be `cursor + 1`; `--to-uid` must be ≤ `UIDNEXT-1`. The cursor moves only if the whole range was fetched and every message resolved. |
| Blocking outcomes | `fetch_error`, `parse_error` always; `no_reservation` unless `--accept-unmatched` (those emails are skipped for good once the cursor passes them). Dry-run lists them as "apply would be REFUSED". |
| Stored data | `GuestInboundMessage` + canonical dual-write via `ingest_parsed_email(side_effects=False)`, with the original `Date` (fallback: IMAP INTERNALDATE, never "now"). |
| Never happens | LLM, auto-replies (e-mail/WhatsApp/Booking), push, language/thread-state update, arrival/invoice/parking handlers, reservation field changes. |
| After commit | One `touch_reservation_version(MESSAGES)` per affected reservation (a failed bump is reported; the data stays). |
| Concurrency | `--apply` holds the tenant IMAP lock; it is refused while a poll runs, and polls skip while it runs. Dry-run takes no lock. |
| Size | Range limited to 200 UIDs unless `--allow-large-range`. |

## Live IMAP polling safeguards

Applies to `poll_guest_email_inbox` (Celery) and the `poll_guest_email` CLI. **The Celery task is registered in the
worker but gated by `GUEST_IMAP_CELERY_POLL_ENABLED` (default `false`): it does nothing until the flag is switched on
following [Enabling the Celery poll](#enabling-the-celery-poll-guest_imap_celery_poll_enabled).**

| Safeguard | Behaviour |
|---|---|
| UIDVALIDITY | `TenantReceptionSettings.guest_imap_uidvalidity` (nullable, 32-bit values). Every poll reads `STATUS (UIDVALIDITY UIDNEXT)` **before** selecting INBOX. A different value ⇒ the poll **refuses**, logs `ERROR … REFUSED`, records a `GuestImapIssue` (uid=0) and leaves cursor and stored value untouched. Nothing is ever reset automatically. A refused poll leaves no ingest stamp, so the lag in system status keeps growing. |
| Not initialised | A tenant with a cursor but **no** stored UIDVALIDITY is refused (not auto-adopted: the server's current value could belong to a replaced mailbox). Only a tenant with cursor 0 adopts the value on its first poll. |
| Cursor sanity | Cursor > `UIDNEXT-1` (mailbox smaller than the cursor) ⇒ refused. Missing STATUS ⇒ refused. |
| UID search | Bounded `UID cursor+1:UIDNEXT-1` (never `n:*`; IMAP returns the *highest* UID for `n:*` when `n` is above it) and the reply is filtered again. A caught-up cursor does no SEARCH and no FETCH (before, the last message was re-fetched every poll). |
| Cursor movement | The cursor follows the longest prefix of messages that are processed, legitimately skipped, or **quarantined**. A failed FETCH / parse / store, or a booking without a reservation, is written to the `GuestImapIssue` ledger and **holds the cursor** while it is retried by the next polls; it never stops the messages after it (those are stored; dedupe makes re-polls safe, automation runs once per message). After the attempt limit (fetch/store/no-reservation 10, parse 2) the issue is `quarantined`: the cursor moves on, the UID stays in the ledger. Cursor is saved per message, so a killed poll resumes where it died. A ledger write failure stops the poll without moving the cursor. |
| Batch | At most 100 messages per poll (the lock is held for shorter), the rest on the next poll. |
| Ledger content | UID, UIDVALIDITY, kind, status, attempts, exception *type* only: never subjects, addresses or bodies. |

Inspect the ledger: `GuestImapIssue` (`status` = `open` / `quarantined` / `resolved`). Poll CLI output shows
`BLOCKED=…`, `issues_open=`, `quarantined=`; the task summary has `blocked`, `issues_open`, `quarantined`.

### Initialise UIDVALIDITY for an existing cursor (operator, once)

```bash
# 1. Dry-run: reads the mailbox read-only and prints the server values. Compare the cursor message's INTERNALDATE
#    with the newest ingested e-mail's received_at: they should be about the same moment.
docker compose --profile test-run run --rm django-run python manage.py init_guest_imap_uidvalidity \
  --tenant uzorita --expect-uidvalidity <UIDVALIDITY> --expect-cursor <CURSOR>
# 2. Same command with --apply (stores it; the server must confirm the asserted value, cursor must not be beyond the mailbox).
```

A different stored value is never overwritten unless `--replace-from <old>` is given — and replacing it means the cursor
no longer matches that mailbox: decide the cursor deliberately (replay) before polling.

### Retrying recorded messages

`retry_guest_imap_issues [--uid N] [--apply] [--with-automation]` re-fetches `open`/`quarantined` UIDs read-only, stores them
**without** the live automation by default (they are late) and marks the issue `resolved`; it never moves the cursor, refuses
if the server UIDVALIDITY differs from the stored one and holds the tenant lock while applying. Issues recorded under another
UIDVALIDITY are reported `stale_uidvalidity` and left alone.

**Checklist before enabling the Celery poll:** (1) UIDVALIDITY initialised and verified;
(2) approved real dry-run + `replay_guest_email --apply` of the backlog done, cursor = last UID; (3) task registered and
flag still off (PR-3); (4) follow the procedure below.

### Enabling the Celery poll (`GUEST_IMAP_CELERY_POLL_ENABLED`)

Two independent switches, both must be on for a tenant to be polled by Celery:

| Switch | Scope | Default |
|---|---|---|
| `GUEST_IMAP_CELERY_POLL_ENABLED` (env) | whole worker process | `false` |
| `TenantReceptionSettings.guest_imap_enabled` (+ SMTP password, tenant `active`) | per tenant | per tenant |

With the flag off the task returns `{"disabled": true}` as its first statement: no DB query, no IMAP connection, no lock,
no cursor write, no automation. The Beat entry `guest-email-imap-poll` (every 120 s, `tenant_slug=uzorita`) keeps firing;
that is harmless and logs `guest email imap poll skipped: GUEST_IMAP_CELERY_POLL_ENABLED is off` at INFO. The CLI
`poll_guest_email` is **not** gated by the flag. Registration is not activation: the other Beat entries that are not
registered in the worker (Channex feeds/reviews, overbooking scans, the two report e-mails) are untouched by PR-3.

**0. Verify the production worker after the deploy (flag still off)**

```bash
docker compose exec -T celery-worker celery -A config inspect registered | grep poll_guest_email_inbox   # must list it
docker compose exec -T celery-worker printenv GUEST_IMAP_CELERY_POLL_ENABLED                              # empty or false
docker compose logs --since 10m celery-worker | grep -c "Received unregistered task"                      # 0 for this task
docker compose logs --since 10m celery-worker | grep "imap poll skipped"                                  # one line per 120 s
```

**1. Pre-flight (read-only):** `init_guest_imap_uidvalidity` dry-run reports `stored_uidvalidity` = server value and the
cursor present on the server; `guest_imap_enabled` is set only for the tenants that should be polled.

**2. Switch on:** set `GUEST_IMAP_CELERY_POLL_ENABLED=true` in `.env`, then recreate (never `restart`, it keeps the stale env):

```bash
docker compose up -d django celery-worker
docker compose exec -T celery-worker printenv GUEST_IMAP_CELERY_POLL_ENABLED   # true
```

**3. Watch the first polls:** `docker compose logs -f celery-worker | grep "imap poll"` shows `guest email imap poll finished`
with `blocked_reason`, `issues_open`, `quarantined`; `GET …/system/status/` → `conversation.channels.email` should show a
fresh `last_poll_at` and falling `ingest_lag_seconds`. A `REFUSED` ERROR log means a guard fired (UIDVALIDITY / cursor):
**stop**, do not reset anything by hand (see the sections above).

**Switch off (rollback):** set `GUEST_IMAP_CELERY_POLL_ENABLED=false` (or remove it) and run
`docker compose up -d django celery-worker`. A poll already running finishes under its lock; the next Beat tick is a no-op.
To stop a single tenant without touching the process, set its `guest_imap_enabled=false`. Verify with the step-0 commands
(`printenv` empty/false, `imap poll skipped` back in the log). Cursor, UIDVALIDITY and the issue ledger are never changed
by switching the flag.

### Single-flight IMAP lock

`poll_tenant_guest_inbox` (Celery task **and** `poll_guest_email` CLI) and `replay_guest_email --apply` share a
non-blocking Postgres session advisory lock per tenant (`apps/communications/guest_imap_lock.py`). A second caller
skips (`locked=1` in the task summary / CLI output; no IMAP session, no ingest stamp). The lock is released in
`finally` and by Postgres if the process dies. Do not run the replay and a live poll "side by side" by working around it.

---

## CLI

```bash
# Poll IMAP inbox za inbound Booking.com mailove
docker compose exec django python manage.py poll_guest_email --tenant=uzorita

# Pošalji poruku preko Channexa
docker compose exec django python manage.py send_channex_booking_message \
  --reservation-id 798 \
  --message-file docs/operations/booking-message-5238895494-pierre-fr.txt \
  --tenant-slug uzorita

# Povuci postojeće poruke iz Channexa (backfill)
docker compose exec django python manage.py sync_channex_booking_messages \
  --tenant-slug uzorita

# Jedna rezervacija
docker compose exec django python manage.py sync_channex_booking_messages \
  --tenant-slug uzorita --reservation-id 798
```

---

## Provjera

| Test | Očekivano |
|------|-----------|
| Gost piše na B.com (email) | Inbound u timeline (IMAP poll); FCM push |
| Gost piše na B.com (Channex) | Inbound u admin → Channex messages; vidljivo u web/Flutter timeline |
| Send `booking` | Poruka u B.com extranet Poruke |
| Send `email` | Mail na gostovu adresu; Sent u webmail |
| Send `whatsapp` | Otvara WhatsApp s predloženim tekstom |
| Vlastita platforma compose | `booking.available=false`, `email.available=true` |

---

## Troubleshooting

| Simptom | Rješenje |
|---------|----------|
| Send booking → 403 | Messages & Reviews app nije aktivan na Channex propertyju |
| Inbound mail ne stiže | Provjeri `guest_imap_enabled`, SMTP lozinku, `poll_guest_email --tenant=uzorita`; mail mora biti s `@guest.booking.com` |
| Channex/WA/IMAP ingest tih | `GET …/system/status/` → `conversation.channels.*` (`last_webhook_at` / `last_poll_at` / `ingest_lag_seconds`); rastući lag = webhook ili Celery poll nije radio |
| Poruka u Pulseu/mailu, ne u messengeru | Mail/Pulse nisu ingest kanal — samo Channex webhook/API. Provjeri `external_id` (`channex:uuid` vs samo booking code), CLI `sync_channex_booking_messages --reservation-id` (ne UI `sync=1`), Channex Messages & Reviews app |
| `booking.available=false` | Rezervacija nije `import_source=channex` ili nema Channex linka (UUID, booking code ili revision) |
| Mail ne odlazi | Tenant SMTP u Reception settings (`guest_contact_email` + password) |

---

## Povezano

- [ADR 0019 — Conversation store](../architecture/adr/0019-messaging-conversation-store.md)
- [guest-messages-flutter.md](../development/guest-messages-flutter.md) — Flutter implementacija
- [channex-uzorita-booking-channel.md](../integrations/channex-uzorita-booking-channel.md)
- [whatsapp-checkin-template.md](./whatsapp-checkin-template.md)
