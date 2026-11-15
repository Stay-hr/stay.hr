# WhatsApp media archive (PR 1)

Durable private copy of every media object that arrives via WhatsApp: guest and
operator messages (Cloud API webhook) and Business App echoes (`smb_message_echoes`).
Images, documents, video, audio/voice notes and stickers.

**Scope of PR 1:** archive + retry + ops command. Nothing is deleted — not on Meta
(PR 2), not on disk (retention is *recorded and reported only*). No read API/UI yet (PR 3).

## Flow

```text
webhook → WhatsAppMessage (unchanged) → WhatsAppMediaArchive(pending)  [DB only]
        → on_commit → Celery archive_whatsapp_media
             claim (lease) → stream to private storage → verify size + sha256 → archived
             failure → failed + next_retry_at ; the beat sweeper (60 s) is the ONLY retry scheduler
```

- **Idempotency:** `UNIQUE(message, media_id)`. A redelivered webhook never creates a second
  row or enqueue; a redelivery also heals a message whose row was never created.
  FK (not OneToOne) so a message may later carry more than one media reference.
- **Never breaks ingestion:** row creation runs in a savepoint inside `try/except`; a broker
  outage leaves the row `pending` and the sweeper (every 5 min) enqueues it.
- **Integrity:** Meta's `sha256` / `file_size` from the media metadata are compared with the
  downloaded bytes; mismatch → failure, nothing marked archived. DB `CHECK` forbids
  `archived` without file + sha256.
- **Statuses:** `pending → downloading → archived | failed (retry) | permanent_failed`.
  404/410 (media expired, Meta keeps ~30 days), over-limit files and malformed `media_id`s are
  permanent; everything else retries with backoff (1m, 5m, 30m, 2h, 6h, 12h, 24h) up to
  `WHATSAPP_MEDIA_MAX_ATTEMPTS`.
- **`media_id` validation:** must match `^[A-Za-z0-9_-]{1,128}$` (it becomes a URL path segment).
  Anything else is logged (length only) and gets no archive row; the message is still stored.
- **Independent of document intake:** `process_inbound_message`, `DocumentIntakeImage`, batch and
  operator flows are untouched. The archive is the raw record; intake is processing. In PR 1
  intake and archive each download from Meta (accepted cost of keeping them separate).

## Ordinary media vs. personal documents

| `sensitivity` | Kinds | Storage prefix |
|---|---|---|
| `standard` | video, audio, voice, sticker | `whatsapp_media/standard/…` |
| `personal_document_candidate` | image, document | `whatsapp_media/personal/…` |

The system cannot tell an ID photo from a luggage photo, so every image/document is treated as a
potential personal document. The prefix lets later phases apply stricter access, audit and
retention without moving files. Guest-supplied filenames are stored in the DB only; the disk
path is server-built (`{archive_id}-{sha12}{whitelisted ext}`).

## Storage and privacy

- `PRIVATE_MEDIA_ROOT` (default `/app/backend/private_media`, bind `./data/private_media`).
  **Not** under `MEDIA_ROOT`: Traefik routes `/media` of `booking.uzorita.hr` to Django.
- `PrivateMediaStorage` has no URL (`.url` raises); files `0600`, dirs `0700`.
- Archive tenant = WABA tenant (`platform`). Future read access must authorise via
  `message.reservation.tenant_id` (see AGENTS.md *Document intake — tenant invariant*).

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `WHATSAPP_MEDIA_ARCHIVE_ENABLED` | `false` | Master switch (webhook hook, task, sweeper, command). |
| `PRIVATE_MEDIA_ROOT` | `/app/backend/private_media` | Private storage root. |
| `WHATSAPP_MEDIA_MAX_BYTES` | 100 MiB | Per-file cap (declared size and streamed size). |
| `WHATSAPP_MEDIA_MAX_ATTEMPTS` | 8 | Then `permanent_failed`. |
| `WHATSAPP_MEDIA_MAX_TOTAL_BYTES` | 50 GiB | Archive size cap; `0` = off. Over cap → downloads deferred 1 h, not failed. |
| `WHATSAPP_MEDIA_MIN_FREE_BYTES` | 20 GiB | Defer downloads when free disk is lower; `0` = off. |
| `WHATSAPP_MEDIA_RETENTION_DAYS_STANDARD` | `0` | Sets `retention_until`; `0` = keep. |
| `WHATSAPP_MEDIA_RETENTION_DAYS_PERSONAL` | `0` | Same, personal candidates. **Needs a legal basis first.** |

Quota/low-disk deferrals log at `ERROR` and do not consume an attempt.

After `.env` changes: `docker compose up -d django celery-worker celery-beat` (not `restart`).

## Retention

Only `retention_until` is set. **No code deletes archived files.** Report what would be due:

```bash
docker compose --profile test-run run --rm django-run python manage.py whatsapp_media_archive --retention-report
```

Deleting personal documents requires an agreed legal basis and deadlines (open decision).

## Operations

Use `django-run` (never `docker compose run django`; see AGENTS.md).

```bash
… manage.py whatsapp_media_archive --stats
… manage.py whatsapp_media_archive --retry-failed [--include-permanent] [--inline]
… manage.py whatsapp_media_archive --backfill --since 2026-11-01 [--inline]
… manage.py whatsapp_media_archive --id 123
… manage.py whatsapp_media_archive --reconcile-orphans [--delete]
```

`--backfill` creates rows for existing media messages; Meta has already expired media older than
~30 days, those end as `permanent_failed` (404) — that is expected, not an incident.
`--include-permanent` resets attempts (e.g. after fixing an expired access token or the
storage permissions).

## Retry mechanisms — how they fit together

| Mechanism | Trigger | Gate / bound |
|---|---|---|
| First attempt | `transaction.on_commit` after the webhook stored the row | — |
| Lost first attempt | sweeper: `pending` older than 300 s | grace > sweep interval, so the first attempt wins |
| Backoff retry | sweeper: `failed` with `next_retry_at <= now` | claim refuses a `failed` row that is not due (`not_due`) |
| Abandoned claim | sweeper: `downloading` with expired lease | claim counts as an attempt; at `MAX_ATTEMPTS` the row becomes `permanent_failed` |
| Quota / unwritable storage | claim-time check, or `EACCES/EPERM/ENOSPC/EROFS/EDQUOT` mid-attempt | row deferred 1 h, **attempt not consumed**, logged at `ERROR` |
| Operator | `--retry-failed`, `--id` (make rows due first; the attempt cap still applies) | `--include-permanent` also resets attempts |

Rules that keep this coherent (each pinned by `test_whatsapp_media_archive_retry_safety.py`):

- **No countdown/ETA tasks.** Redis' default `visibility_timeout` is 1 h, shorter than our longest
  backoff, so ETA tasks would be redelivered and duplicated. Retries exist only as row state.
- **Every claim counts.** A duplicate/redelivered task cannot shorten a backoff, and a "poison" row
  that keeps killing workers (OOM, SIGKILL) ends as `permanent_failed` after `MAX_ATTEMPTS` claims.
- **Timing contract:** download deadline 480 s < Celery soft limit 540 s < hard limit 600 s <
  row lease 900 s; beat interval = sweep interval (60 s). A live worker's claim therefore cannot
  expire under it. Constants live in `media_archive.py`, not in env, so they cannot be mis-tuned.

## Crash recovery and orphan files

Files are written to an attempt-unique `*.part` file and moved into place with `os.replace` under a
**deterministic** name (`{archive_id}-{sha12}.ext`; the storage allows overwrite). Consequences:

- readers never see a partial file; a retry or a concurrent attempt rewrites the same path, so a
  crash between the file write and the DB update does **not** leave a duplicate behind;
- `archived` is only recorded if the published file exists;
- in-process code never deletes a published file (a concurrent attempt may be using the same
  path). Only a failed attempt's own `.part` is removed.

What *can* remain: an unreferenced file (row ended `permanent_failed`) or a `.part` after a hard
kill. Find them — report only by default:

```bash
… manage.py whatsapp_media_archive --reconcile-orphans [--min-age-hours 24]
… manage.py whatsapp_media_archive --reconcile-orphans --delete   # explicit; unreferenced files only
```

The report also lists `archived` rows whose file is missing. Nothing runs this from beat.

## File cleanup when rows are deleted (CASCADE)

Deleting a `WhatsAppMessage`, a tenant, or archive rows directly removes the rows (CASCADE is
kept). The files are cleaned **after commit** through a deletion outbox, never inside the
transaction:

1. `post_delete` writes a `WhatsAppMediaFileDeletion` row in the *same* transaction (rollback ⇒ no
   request, file untouched) and registers `transaction.on_commit(enqueue)`. If the outbox cannot be
   written, the deletion itself fails rather than stranding a document.
2. Task `cleanup_whatsapp_media_file` deletes the file: idempotent (missing file = done), refuses
   paths outside `whatsapp_media/`, and **skips a file another archive row still references**
   (`skipped_in_use`).
3. Beat sweeper (5 min) re-enqueues `pending` outbox rows, so Redis/Celery downtime only delays
   cleanup. Failures back off (1 m … 24 h); after 12 attempts the row is `dead` for an operator.

Works regardless of `WHATSAPP_MEDIA_ARCHIVE_ENABLED`. Not covered: raw SQL `DELETE`/`TRUNCATE`
(no Django signal) — use `--reconcile-orphans` after such operations. This is **not** retention:
no file is removed unless its DB row was deleted.

## Host prerequisites (before enabling)

The container runs as uid 1000. A directory Docker created as `root:root` is not writable and every
download would be deferred (logged at `ERROR`, never burning attempts).

```bash
mkdir -p data/private_media && sudo chown 1000:1000 data/private_media && sudo chmod 700 data/private_media
```

Guards: Django check `integrations.E001` fails startup if `PRIVATE_MEDIA_ROOT` overlaps
`MEDIA_ROOT`/`STATIC_ROOT`; `integrations.W001` warns when the archive is enabled but the root is
not writable; `whatsapp_media_archive --stats` prints `writable=`. `data/private_media` is in
`.dockerignore` (documents must not enter the build context).

## Logging

No access tokens, signed download URLs, captions, filenames or file contents are logged. The
Bearer token only travels in a request header, and `httpx`/`httpcore` request lines (which contain
Meta's signed `lookaside.fbsbx.com` URLs) are held at `WARNING` in `LOGGING`. `last_error` stores
Meta's error text (≤ 500 chars) in the DB only.

## Backup and disk — READ BEFORE ENABLING

- **No backup job for stay.hr was found on the host** (cron, systemd timers, containers).
  `data/private_media/` is therefore **not backed up** until ops adds it. Archived media is the
  only copy once PR 2 deletes it on Meta — do not enable PR 2 before a verified backup exists.
- Disk at the time of writing: 78 % used, ~430 GB free; `data/media` ≈ 1.5 GB. Video/audio can
  change that quickly; tune `MAX_TOTAL_BYTES` / `MIN_FREE_BYTES` and watch `--stats`.

## Rollout

1. Merge PR 1 (deploys dark: `WHATSAPP_MEDIA_ARCHIVE_ENABLED=false`; migration is additive).
2. Create `data/private_media` on the host and add it to backup.
3. Enable on a non-production tenant / staging, send test media, check `--stats`.
4. Enable in production only after review. PR 2 (Meta deletion) and PR 3 (authorised read) follow.
