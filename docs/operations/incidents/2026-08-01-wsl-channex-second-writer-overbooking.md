# Incident: WSL second-writer Channex ARI overbooking (2026-08-01)

**Status:** Incident closed · fully remediated (root cause + residual exposure)  
**Closed after:** Full inventory reconcile on hel1 · Verify clean (0 mismatches) ~21:28 UTC  
**Related:** [ADR 0014 — Channex outbound guard](../../architecture/adr/0014-channex-outbound-guard.md) · [WSL Channex safe mode](../wsl-channex-safe-mode.md) · [Multi-room overbooking checklist](../multi-room-overbooking-checklist.md)

---

## Summary

A **WSL** stay.hr instance with a **stale database** ran periodic
`verify_channex_availability_daily` with **`repair=True`**, pushed
`availability=1` for unit **R4** to **live** Channex, and Booking.com sold
overlapping stays. Booking.com / Channex behaved correctly given open inventory;
stay.hr (non-authoritative writer) was the source.

The incident has **two phases**: removing the writer did not by itself close
nights already opened on Channex. A later booking (`#1065`) was **residual
exposure**, not a new root cause.

---

## Two phases

| Phase | Name | What happened |
|-------|------|----------------|
| **A** | **Root cause** | WSL second writer + stale DB + Beat `repair=True` opened R4 inventory → `#1059` Mihailov (1.8.–2.8.), then cascade `#1060` / `#1061`. |
| **B** | **Residual exposure** | Root cause removed (PR #31); remote ARI still not fully reconciled → `#1065` Trojan Danny (2.8.–3.8.) OVERBOOKING vs `#1053` Javier. **No new outbound write** after merge caused this. |

**For future readers:** `#1065` is not a second incident and not proof that PR #31
failed. It sold nights that were **already open** on Channex before the merge.
PR #31 removes the ability to perform new bad outbound writes, but it does not
change inventory already published to Channex. That requires an explicit **full
inventory reconcile**.

---

## Timeline (UTC, 2026-08-01)

| Time | Event |
|------|--------|
| Pre-incident | Production had Javier `#1053` on R4 (`checked_in` 31.7.–3.8.). WSL DB lagged (max reservation id 1051; no Javier). |
| ~07:14 | **Phase A:** WSL Celery `verify_channex_availability_daily`: R4 `expected=1` (local) vs Channex `actual=0` → **repaired** (POST availability). |
| ~07:39 | Booking.com **6163059892** (Mihailov Andreea) Standard King / R4 → hel1 `#1059`. |
| ~08:29–08:40 | Further same-day cascade: `#1060`, `#1061` (later cancelled). |
| Detection | Operator reported R4 overbooking; WSL missing `#1053`/`#1059` until prod DB dump/restore. |
| Morning | Ops handling / cancellations; code fix: fail-closed outbound, verify ≠ repair (PR #31). |
| Deploy | PR #31 on hel1; WSL `CHANNEX_OUTBOUND_ENABLED=false`. **Root cause** removed; **full inventory reconcile** not yet completed. |
| ~20:20 | **Phase B:** `#1065` Trojan Danny expected 2.8.–3.8. ingested with `OVERBOOKING` vs `#1053`. hel1 closed 2.8. on ingest; night had remained open from morning WSL open. |
| ~21:28 | hel1 verify: 4 mismatches (R4 `expected=0` / Channex `1` on 8.8., 12.8., 15.8., 10.9.). Batched `--repair` (threshold) → **Verify clean (0 mismatches)**. |
| Close | **Incident closed** after full inventory reconcile + verify clean. |

---

## Root cause

1. **WSL became a second writer** against the same live Channex property
   (`CHANNEX_OUTBOUND_ENABLED` default was fail-open `True`; WSL `.env` unset;
   documented `docker-compose.dev.yml` missing from repo).
2. **Verify and repair were one operation** with Beat `repair=True`.
3. **Local DB was not authoritative** — repair used stale “expected” availability.

---

## Detection gap

| Question | Answer |
|----------|--------|
| Why was the incident not closed immediately after the merge? | **Root cause** was removed, but **full inventory reconcile** of remote ARI was not run yet. |
| How do we prevent that? | Do not mark **Incident closed** until **Verify clean (0 mismatches)**. |

Removing the writer stops *new* bad writes. It does not heal inventory already
wrong on Channex/Booking. Closing early left residual sellable nights → `#1065`.

---

## Contributing factors

- Stale WSL ↔ hel1 database (no automated dump sync).
- Periodic Beat on WSL with write capability to live Channex.
- No blast-radius threshold (10 R4-related mismatches repaired in one run).
- No hard fail based on runtime role (`DEBUG=False` on WSL via production settings).
- ADR 0014 intended read-only WSL but was not operationally enforced.
- After deploy: no mandatory **full inventory reconcile** before treating the incident as closed.

---

## Impact

- **Phase A:** Overbooking on **R4** for night **2026-08-01** (`#1053` + `#1059`; cascade `#1060` / `#1061`).
- **Phase B:** Overbooking on **R4** for night **2026-08-02** (`#1053` + `#1065`) from leftover open inventory.
- Trust risk: any outbound (rates, stop-sell, photos, ARI) could be corrupted the same way while a second writer exists.

---

## Corrective actions

| Action | Status |
|--------|--------|
| Fail-closed `CHANNEX_OUTBOUND_ENABLED` default `False` | Done (PR #31) |
| `assert_can_write` sole gate + audit + force WARNING + startup banner | Done (PR #31) |
| Beat = verify + notify only; CLI `--repair` explicit | Done (PR #31) |
| Blast-radius threshold before repair | Done (PR #31) |
| Metrics on `/system/status` → `channex.*` | Done (PR #31) |
| Restore `docker-compose.dev.yml` + WSL `.env` false | Done (PR #31) |
| Regression test (stale local free vs remote closed → no POST) | Done (PR #31) |
| **Full inventory reconcile** after bad outbound writes (ops) | Done (hel1 ~21:28 UTC) |
| Redis writer lease / DB generation watermark | Backlog (ADR non-goals) |
| Generic multi-provider OutboundGuard | Backlog |

---

## Breaking operational change

`manage.py verify_channex_availability` is **verify-only** by default.
Pass `--repair` on the **writer** host (`CHANNEX_OUTBOUND_ENABLED=true`) to
re-push. Do not assume bare command still repairs.

---

## Rollout (hel1 — avoid stuck read-only)

1. Merge PR (code default outbound `false`).
2. **Before deploy:** confirm hel1 `.env` has `CHANNEX_OUTBOUND_ENABLED=true`.
3. Deploy (`git pull` + `./scripts/deploy.sh` / `docker compose up -d` django + celery).
4. Confirm startup banner `mode=writer`.
5. Confirm `GET /api/v1/reception/system/status/` → `channex.write_enabled: true`.
6. Run verify-only: `manage.py verify_channex_availability --tenant-slug uzorita`.
7. Only then, if needed: `--repair` after reading threshold output.

**WSL:** `CHANNEX_OUTBOUND_ENABLED=false`, recreate containers; banner `mode=read-only`; Beat must not POST availability.

---

## After bad outbound writes — close procedure

Removing **root cause** is not enough. Before **Incident closed**:

```text
stop writer
→ remove root cause / deploy
→ FULL INVENTORY RECONCILE (hel1 writer)
→ Verify clean (0 mismatches)
→ Incident closed
```

Commands (**hel1 writer only**):

```bash
docker compose exec django python manage.py verify_channex_availability --tenant-slug uzorita
# On mismatches: --repair in windows ≤ CHANNEX_ARI_REPAIR_MAX_DAYS_PER_UNIT (default 3),
# or narrower --from-date / --days until verify is clean
docker compose exec django python manage.py verify_channex_availability \
  --tenant-slug uzorita --repair --from-date YYYY-MM-DD --days N
```

Bare `verify` is verify-only. Blast-radius threshold may skip a large `--repair`;
batch by day (as on 2026-08-01 evening) until **Verify clean (0 mismatches)**.

### Incident Done

```text
Incident Done

☐ Root cause uklonjen
☐ Produkcija deployana
☐ Writer potvrđen
☐ Full inventory reconcile završen
☐ Verify clean (0 mismatches)
☐ Nema otvorenog inventara na zahvaćenim noćima
☐ Monitoring stabilan 24 h
☐ Incident closed
```

---

## Lessons

> A process whose local database is not authoritative must not perform write
> operations against the production channel manager.

Verify must be safe everywhere; repair only on an authorized writer after guards
and blast-radius checks. The same pattern applies to future outbound providers
(Booking Direct, Expedia, Airbnb, …).

> Removing **root cause** ≠ healing **residual exposure**. After any bad
> outbound writes to Channex, run **full inventory reconcile** and require
> **Verify clean (0 mismatches)** before **Incident closed**.
