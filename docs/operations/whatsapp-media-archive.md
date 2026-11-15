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
             failure → failed + backoff (countdown) ; beat sweeper = safety net
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
  404/410 (media expired, Meta keeps ~30 days) and over-limit files are permanent; everything
  else retries with backoff (1m, 5m, 30m, 2h, 6h, 12h, 24h) up to
  `WHATSAPP_MEDIA_MAX_ATTEMPTS`.
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
```

`--backfill` creates rows for existing media messages; Meta has already expired media older than
~30 days, those end as `permanent_failed` (404) — that is expected, not an incident.
`--include-permanent` resets attempts (e.g. after fixing an expired access token).

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
