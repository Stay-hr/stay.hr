# ADR 0014 — Channex outbound guard (single-writer concurrency control)

**Status:** Accepted  
**Date:** 2026-07-29  
**Authors:** Platform team

## Context

stay.hr is in active development and **controlled production testing** on Uzorita
(real guests, real Channex property). The WSL dev environment shares the same
Channex API key as the production server (hel1).

Two writers issuing ARI pushes, booking ACKs, or revision ingests against the
same Channex property causes:

- Double ACKs (feed appears empty for the other writer)
- Conflicting ARI repair (availability flip-flops)
- Unexplained inventory changes

This is **concurrency control**, not a security feature.

## Decision

```text
             HEL1
        WRITE + READ
              │
────────────Channex────────────
              │
          WSL DEV
         READ ONLY
```

| Host | Role | `can_write_to_channex()` |
|------|------|--------------------------|
| **hel1** | Single live writer — deploy, ARI, mapping, new room | **true** |
| **WSL** | Local API/UI, read, dump | **false** |

### Implementation layers

1. **Env flag** `CHANNEX_OUTBOUND_ENABLED` (default `True` → hel1 unchanged).
2. **Guard helper** `can_write_to_channex()` in `apps.integrations.channex.outbound_guard`.
3. **Client choke** in `ChannexClient._request`: non-GET + write disabled → raise `ChannexWriteDisabled`.
4. **Early-skip** in periodic Celery tasks: return `{"skipped": true}` (not an error).
5. **ACK atomicity**: if write disabled → no ingest, no ACK (never ingest then fail ACK).
6. **Force override** via `force_channex_write()` context manager for CLI maintenance
   (`--force-channex-outbound`).

### Skip vs raise

| Path | Behaviour when write disabled |
|------|-------------------------------|
| Celery Beat / worker tasks | `return {"skipped": True}` + INFO log |
| `ChannexClient` non-GET (safety net) | raise `ChannexWriteDisabled` |
| Management command without `--force-…` | raise `CommandError` with hint |
| Management command with `--force-…` | WARNING + structured log `reason=force_cli` + proceed |

### Observability

- Structured log event `channex_outbound_blocked` with method, endpoint, reason, tenant.
- In-process counter `channex_outbound_blocked_total` exposed in `GET /api/v1/reception/system/status/`.
- Worker startup WARNING when `CHANNEX_OUTBOUND_ENABLED=false`.

### WSL defaults

- `docker-compose.dev.yml` sets `CHANNEX_OUTBOUND_ENABLED: "false"` for django, celery-worker, celery-beat.
- `scripts/up-dev.sh` starts without Celery by default; `--with-celery` for intentional queue testing.

## Consequences

- hel1 remains the **single writer** with no `.env` change required.
- WSL can freely run API/UI, read Channex data, run tests, without risking live channel state.
- Explicit `--force-channex-outbound` on CLI commands enables maintenance writes from WSL when hel1 is offline.

## Release notes

**Introduced single-writer concurrency control for Channex integrations.**

Development environments are now read-only by default. All outbound writes are
centrally enforced through `ChannexClient`, while operational tasks degrade
gracefully via early-skip. This prevents concurrent writers against the same
live Channex account without affecting read operations.

## Non-goals

- Separate staging Channex property
- hel1→WSL dump/restore automation
- Disabling live testing on hel1
