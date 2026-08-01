# ADR 0014 — Channex outbound guard (single-writer concurrency control)

**Status:** Accepted (amended 2026-08-01)  
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
- Unexplained inventory changes / **overbooking**

This is **concurrency control**, not a security feature.

**Incident 2026-08-01:** WSL Celery ran `verify_channex_availability_daily` with
`repair=True` against a stale DB, pushed `availability=1` for R4 to live Channex,
and Booking.com sold a same-day stay → overbooking `#1053` / `#1059`. See
[postmortem](../../operations/incidents/2026-08-01-wsl-channex-second-writer-overbooking.md).

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

| Host | Role | `CHANNEX_OUTBOUND_ENABLED` |
|------|------|----------------------------|
| **hel1** | Single live writer — deploy, ARI, mapping | **`true` (must set in `.env`)** |
| **WSL** | Local API/UI, read, dump, **verify-only** | **`false` (code default)** |

### Fail-closed default

`CHANNEX_OUTBOUND_ENABLED` defaults to **`False`**. Omitting the flag on hel1
after deploy makes production read-only — set `true` **before** deploy
(see rollout in the postmortem).

WSL also uses `DJANGO_SETTINGS_MODULE=config.settings.production` (`DEBUG=False`);
hard-deny on `DEBUG` alone would not have blocked the incident.

### Verify ≠ repair

Shared diff engine only:

```text
verify() → mismatch_list
repair(mismatch_list) → OutboundGuard → blast-radius threshold → write
```

- Daily Beat: **verify + notify only** (safe on every host).
- Repair: CLI `--repair` on an authorized writer (subject to guard + threshold).
- Repair must **not** recompute availability with a different formula.

### Blast-radius threshold (repair only)

Refuse repair (structured skip log) when any of:

- distinct units with mismatches ≥ `CHANNEX_ARI_REPAIR_MAX_UNITS` (default 5)
- affected units / units_checked > `CHANNEX_ARI_REPAIR_MAX_UNIT_PERCENT` (default 20%),
  only when `units_checked >= max_units` (avoids 1/1 false trips)
- any unit has ≥ `CHANNEX_ARI_REPAIR_MAX_DAYS_PER_UNIT` mismatch days (default 3)

### Implementation layers

1. **Env flag** `CHANNEX_OUTBOUND_ENABLED` (default **`False`** — fail-closed).
2. Optional `CHANNEX_OUTBOUND_TENANT_SLUGS` allowlist; `CHANNEX_OUTBOUND_MAINTENANCE`.
3. **`assert_can_write()`** in `outbound_guard` — sole semantic write gate; audits every
   allow/block (`channex_outbound_decision`); force emits `CHANNEX FORCE WRITE` WARNING.
4. **Client choke** in `ChannexClient._request`: non-GET → `assert_can_write`.
5. **Early-skip** for periodic *write* Celery tasks (`skip_if_channex_write_disabled`).
   Verify-only Beat tasks do **not** early-skip.
6. **ACK atomicity**: if write disabled → no ingest, no ACK.
7. **Force override** via `force_channex_write()` / `--force-channex-outbound`.
8. Startup banner: `Channex outbound: enabled=… mode=writer|read-only`.
9. Process counters on `GET /system/status` → `channex.*`.

### Skip vs raise

| Path | Behaviour when write disabled |
|------|-------------------------------|
| Celery *write* Beat/worker tasks | `return {"skipped": True}` + audit log |
| Celery **verify** daily | runs (GET + notify); never POSTs |
| `ChannexClient` non-GET | raise `ChannexWriteDisabled` |
| Management command without `--force-…` | raise `CommandError` with hint |
| Management command with `--force-…` | `CHANNEX FORCE WRITE` WARNING + proceed |

### Breaking operational change (CLI)

- **Before:** `manage.py verify_channex_availability` repaired by default.
- **After:** bare command is **verify-only**; pass `--repair` on the writer host.

### Redis / distributed writer lease (deferred)

Single-writer in v1 is enforced by configuration (`CHANNEX_OUTBOUND_ENABLED`) and
operational rules because there is **only one authorized production instance**.
A distributed lease/lock is **not** required yet. If multiple writer instances are
introduced later, add a distributed writer lease — consciously deferred, not forgotten.

## Consequences

- hel1 must set `CHANNEX_OUTBOUND_ENABLED=true` in `.env` before every deploy that
  includes this change.
- WSL can run API/UI, read Channex, run verify, dump/restore DB, without mutating
  live ARI — even if Celery beat is running.
- Explicit `--force-channex-outbound` enables maintenance writes from WSL only when
  hel1 is offline.

## Release notes

**Amended single-writer concurrency control (fail-closed + verify ≠ repair).**

- Default outbound disabled; hel1 opts in via `.env`.
- Availability verify and repair are separate; Beat never auto-repairs.
- OutboundGuard audits every decision; blast-radius threshold blocks suspicious
  mass repairs (stale-DB proxy).
- CLI: `--repair` required to push ARI.

## Non-goals

- Redis single-writer lock / lease (deferred — see above)
- DB dump generation / freshness watermark before repair
- Generic multi-provider `OutboundGuard` (Channex-first; extract when Booking
  Direct / Expedia / Airbnb outbound appears)
- Separate staging Channex property
- Disabling live testing on hel1
