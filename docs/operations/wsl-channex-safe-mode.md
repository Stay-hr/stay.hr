# WSL Channex safe mode (read-only)

**Release note:** Introduced single-writer concurrency control for Channex
integrations. Development environments are now read-only by default. All
outbound writes are centrally enforced through `ChannexClient`, while
operational tasks degrade gracefully via early-skip. This prevents concurrent
writers against the same live Channex account without affecting read operations.

## Architecture

```text
             HEL1
        WRITE + READ
              │
────────────Channex────────────
              │
          WSL DEV
         READ ONLY
```

| Host | Role | `CHANNEX_OUTBOUND_ENABLED` |
|------|------|----------------------------|
| hel1 | Single live writer | `true` (default) |
| WSL | Local dev, read, dump | `false` |

## How it works

- `can_write_to_channex()` checks the env flag + force context override.
- `ChannexClient._request` blocks non-GET when write disabled → raises `ChannexWriteDisabled`.
- Celery tasks early-skip (return `{"skipped": true}`) — not an error.
- ACK atomicity: no ingest without ACK capability.

## Checklist (WSL)

1. **`.env`** contains `CHANNEX_OUTBOUND_ENABLED=false`
2. `docker-compose.dev.yml` sets the same for django/celery services
3. Start with `./scripts/up-dev.sh` (no Celery by default)
4. Verify: `GET /api/v1/reception/system/status/` → `channex.write_enabled: false`

## Force write (maintenance)

When hel1 is offline and you need to write from WSL:

```bash
python manage.py channex_ari_full_sync --force-channex-outbound --tenant-slug uzorita
python manage.py verify_channex_availability --force-channex-outbound --tenant-slug uzorita
python manage.py channex_booking_revisions_feed --force-channex-outbound --tenant-slug uzorita
python manage.py channex_ari_flush --force-channex-outbound --tenant-slug uzorita
python manage.py cancel_channex_booking --force-channex-outbound --reservation-id 123
python manage.py send_channex_booking_message --force-channex-outbound --reservation-id 123 --message-file msg.txt
```

**Never** use `--force-channex-outbound` while hel1 is running — two writers = conflicts.

## Observability

- Blocked writes logged as `channex_outbound_blocked` (structured, with method/endpoint/reason)
- Counter visible in `GET /api/v1/reception/system/status/` → `channex.outbound_blocked_total`
- Worker startup WARNING when write disabled

## ADR

[ADR 0014 — Channex outbound guard](../architecture/adr/0014-channex-outbound-guard.md)
