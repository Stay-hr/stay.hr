# SSE lifecycle — observation log

Running log for Phase 1 lifecycle instrumentation (AbortSignal, disconnect, registry, invariant). Phase 1 lifecycle is **closed**; this log is optional ongoing diagnostics, not a Phase 2a lock.

**Window opened:** 2026-07-23 (Europe/Zagreb)  
**Invariant:** `opened − closed − active = 0` (`invariant_delta`).  
**Healthy (daily):** `invariant_delta == 0`, no `sse_invariant_breach`, no `WORKER TIMEOUT`, `opened ≈ closed + active`, active rises/falls on canary or real tab use.

**Rule (ADR 0005):** instrumentation stays **permanent** through Redis (2a) and Uvicorn (2b). There is **no** calendar “≥3 PASS days” gate that blocks Phase 2a. If leak/saturation returns → stop and analyze; if it does not recur → continue Phase 2a in parallel.

Runbook: [gunicorn-sse-monitoring.md](gunicorn-sse-monitoring.md)  
Collector: `./scripts/observe-sse-lifecycle.sh --canary --append-log`  
Artifacts: `data/ops/sse-lifecycle-observation/`

| Day | Observed (UTC) | Verdict | opened | closed | breach | invariant_delta | canary | Notes |
|-----|----------------|---------|--------|--------|--------|-----------------|--------|-------|
| 2026-07-23 | 2026-07-23T14:54:25Z | **PASS** | 2 | 2 | 0 | 0 | pass | Day 0 baseline — rise/fall canary closed in ~50s (`close_reason=client_disconnect`); invariant_delta=0; no breach |
| 2026-07-24 | 2026-07-24T09:50:01Z | **PASS** | 26 | 21 | 0 | 0 | skipped | — |
| 2026-07-25 | 2026-07-25T09:50:01Z | **PASS** | 11 | 11 | 0 | 0 | skipped | — |
| 2026-07-26 | 2026-07-26T09:50:01Z | **PASS** | 34 | 34 | 0 | 0 | skipped | — |
| 2026-07-27 | 2026-07-27T09:50:01Z | **PASS** | 0 | 0 | 0 | 0 | skipped | — |
| 2026-07-28 | 2026-07-28T09:50:01Z | **PASS** | 0 | 0 | 0 | 0 | skipped | — |

## Status

**Instrumentation:** keep on (registry, invariant, BFF/Django logs, `/system/status`).  
**Phase 2a:** not blocked by this log — blocked only by an **active** unresolved leak/saturation or missing instrumentation.

### Ops notes

- Daily cron (host): `50 9 * * * /opt/stacks/stay.hr/scripts/observe-sse-lifecycle-cron.sh` → `/var/log/sse-lifecycle-observe.log`
- Token: set `RECEPTION_API_TOKEN` in `/etc/stay/sse-observe.env` (preferred) or keep load-test creds available for the cron wrapper
- Canary wait is ~70s (two SSE heartbeats); use `--canary` when verifying rise/fall explicitly
- Cross-worker totals: `data/media/ops/daily_ops_report/docker_signals.json` (`sse_invariant_breach` is **CRIT** in daily ops)
