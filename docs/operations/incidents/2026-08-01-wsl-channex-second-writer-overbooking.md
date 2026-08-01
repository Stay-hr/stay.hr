# Incident: WSL second-writer Channex ARI overbooking (2026-08-01)

**Status:** Root cause confirmed · Corrective PR (fail-closed outbound + verify ≠ repair)  
**Related:** [ADR 0014 — Channex outbound guard](../../architecture/adr/0014-channex-outbound-guard.md) · [WSL Channex safe mode](../wsl-channex-safe-mode.md) · [Multi-room overbooking checklist](../multi-room-overbooking-checklist.md)

---

## Summary

A **WSL** stay.hr instance with a **stale database** ran periodic
`verify_channex_availability_daily` with **`repair=True`**, pushed
`availability=1` for unit **R4** to **live** Channex, and Booking.com sold a
same-day stay. Production then held two overlapping R4 reservations
(`#1053` Javier Valls Fernandez checked_in 31.7–3.8 + `#1059` Mihailov Andreea
expected 1.8–2.8). Booking.com / Channex behaved correctly given open inventory;
stay.hr (non-authoritative writer) was the source.

---

## Timeline (UTC, 2026-08-01)

| Time | Event |
|------|--------|
| Pre-incident | Production had Javier `#1053` on R4 (`checked_in`). WSL DB lagged (max reservation id 1051; no Javier). |
| ~07:14 | WSL Celery `verify_channex_availability_daily`: R4 `expected=1` (local) vs Channex `actual=0` → **repaired** (POST availability). |
| ~07:39 | Booking.com reservation **6163059892** (Mihailov Andreea) for Standard King / R4, arrival same day → ingested on hel1 as `#1059`. |
| Detection | Operator reported R4 overbooking; WSL missing `#1053`/`#1059` until prod DB dump/restore. |
| Immediate | Confirm occupancy; ops handling of guests. Code fix: fail-closed outbound, verify ≠ repair. |

---

## Root cause

1. **WSL became a second writer** against the same live Channex property
   (`CHANNEX_OUTBOUND_ENABLED` default was fail-open `True`; WSL `.env` unset;
   documented `docker-compose.dev.yml` missing from repo).
2. **Verify and repair were one operation** with Beat `repair=True`.
3. **Local DB was not authoritative** — repair used stale “expected” availability.

## Contributing factors

- Stale WSL ↔ hel1 database (no automated dump sync).
- Periodic Beat on WSL with write capability to live Channex.
- No blast-radius threshold (10 R4-related mismatches repaired in one run).
- No hard fail based on runtime role (`DEBUG=False` on WSL via production settings).
- ADR 0014 intended read-only WSL but was not operationally enforced.

## Impact

- Overbooking on **R4** for night **2026-08-01** (`#1053` + `#1059`).
- Trust risk: any outbound (rates, stop-sell, photos, ARI) could be corrupted the same way.

## Corrective actions

| Action | Status |
|--------|--------|
| Fail-closed `CHANNEX_OUTBOUND_ENABLED` default `False` | This PR |
| `assert_can_write` sole gate + audit + force WARNING + startup banner | This PR |
| Beat = verify + notify only; CLI `--repair` explicit | This PR |
| Blast-radius threshold before repair | This PR |
| Metrics on `/system/status` → `channex.*` | This PR |
| Restore `docker-compose.dev.yml` + WSL `.env` false | This PR |
| Regression test (stale local free vs remote closed → no POST) | This PR |
| Redis writer lease / DB generation watermark | Backlog (ADR non-goals) |
| Generic multi-provider OutboundGuard | Backlog |

## Breaking operational change

`manage.py verify_channex_availability` is **verify-only** by default.
Pass `--repair` on the **writer** host (`CHANNEX_OUTBOUND_ENABLED=true`) to
re-push. Do not assume bare command still repairs.

## Rollout (hel1 — avoid stuck read-only)

1. Merge PR (code default outbound `false`).
2. **Before deploy:** confirm hel1 `.env` has `CHANNEX_OUTBOUND_ENABLED=true`.
3. Deploy (`git pull` + `./scripts/deploy.sh` / `docker compose up -d` django + celery).
4. Confirm startup banner `mode=writer`.
5. Confirm `GET /api/v1/reception/system/status/` → `channex.write_enabled: true`.
6. Run verify-only: `manage.py verify_channex_availability --tenant-slug uzorita`.
7. Only then, if needed: `--repair` after reading threshold output.

**WSL:** `CHANNEX_OUTBOUND_ENABLED=false`, recreate containers; banner `mode=read-only`; Beat must not POST availability.

## Lessons

> A process whose local database is not authoritative must not perform write
> operations against the production channel manager.

Verify must be safe everywhere; repair only on an authorized writer after guards
and blast-radius checks. The same pattern applies to future outbound providers
(Booking Direct, Expedia, Airbnb, …).
