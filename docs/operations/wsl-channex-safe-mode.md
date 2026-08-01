# WSL Channex safe mode (read-only)

**Release note:** Fail-closed single-writer control (ADR 0014, amended after
[2026-08-01 overbooking](incidents/2026-08-01-wsl-channex-second-writer-overbooking.md)).
WSL must never repair live ARI from a stale DB.

## Architecture

```text
             HEL1
        WRITE + READ
              │
────────────Channex────────────
              │
          WSL DEV
         READ + VERIFY ONLY
```

| Host | Role | `CHANNEX_OUTBOUND_ENABLED` |
|------|------|----------------------------|
| hel1 | Single live writer | `true` (**required** in `.env`) |
| WSL | Local API/UI, read, dump, verify | `false` (**code default**) |

## How it works

- `assert_can_write()` is the sole write gate (audit every decision).
- `ChannexClient._request` blocks non-GET when write disabled.
- Celery **write** tasks early-skip; **verify** daily still runs (GET + notify, no POST).
- ACK atomicity: no ingest without ACK capability.
- Startup log: `Channex outbound: enabled=false … mode=read-only`.

## Checklist (WSL)

1. **`.env`** contains `CHANNEX_OUTBOUND_ENABLED=false`
2. Optional: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d`
3. Recreate after `.env` change: `docker compose up -d django celery-worker celery-beat` (not bare `restart`)
4. Verify startup banner `mode=read-only`
5. Verify: `GET /api/v1/reception/system/status/` → `channex.write_enabled: false`
6. Never run `--repair` / `--force-channex-outbound` while hel1 is the live writer

## Availability verify vs repair

```bash
# Safe everywhere (default — Breaking change 2026-08)
python manage.py verify_channex_availability --tenant-slug uzorita

# Writer host only
python manage.py verify_channex_availability --tenant-slug uzorita --repair
```

Bare command **no longer repairs**. Pass `--repair` explicitly on hel1.

## Force write (maintenance)

When hel1 is offline and you need to write from WSL:

```bash
python manage.py channex_ari_full_sync --force-channex-outbound --tenant-slug uzorita
python manage.py verify_channex_availability --force-channex-outbound --repair --tenant-slug uzorita
python manage.py channex_booking_revisions_feed --force-channex-outbound --tenant-slug uzorita
python manage.py channex_ari_flush --force-channex-outbound --tenant-slug uzorita
```

Expect `CHANNEX FORCE WRITE` WARNING in logs. **Never** force while hel1 is running.

## Observability

- `channex_outbound_decision` (allowed/blocked) + `CHANNEX FORCE WRITE`
- `GET /system/status` → `channex.write_enabled`, `outbound_*_total`, verify/repair counters
- Threshold skips: `channex_repair_skipped_threshold` with units / percent / max_days

## ADR

[ADR 0014 — Channex outbound guard](../architecture/adr/0014-channex-outbound-guard.md)
