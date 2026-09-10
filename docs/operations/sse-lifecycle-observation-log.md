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
| 2026-08-02 | 2026-08-02T09:50:01Z | **PASS** | 2 | 2 | 0 | 0 | skipped | — |
| 2026-08-03 | 2026-08-03T09:50:01Z | **PASS** | 55 | 55 | 0 | 0 | skipped | — |
| 2026-08-04 | 2026-08-04T09:50:01Z | **PASS** | 57 | 57 | 0 | 0 | skipped | — |
| 2026-08-05 | 2026-08-05T09:50:01Z | **PASS** | 22 | 22 | 0 | 0 | skipped | — |
| 2026-08-06 | 2026-08-06T09:50:01Z | **PASS** | 25 | 25 | 0 | 0 | skipped | — |
| 2026-08-07 | 2026-08-07T09:50:01Z | **PASS** | 0 | 0 | 0 | 0 | skipped | — |
| 2026-08-08 | 2026-08-08T09:50:01Z | **PASS** | 34 | 33 | 0 | 0 | skipped | — |
| 2026-08-09 | 2026-08-09T09:50:01Z | **PASS** | 13 | 13 | 0 | 0 | skipped | — |
| 2026-08-10 | 2026-08-10T09:50:01Z | **PASS** | 3 | 2 | 0 | 0 | skipped | — |
| 2026-08-11 | 2026-08-11T09:50:01Z | **PASS** | 37 | 37 | 0 | 0 | skipped | — |
| 2026-08-12 | 2026-08-12T09:50:01Z | **PASS** | 2 | 2 | 0 | 0 | skipped | — |
| 2026-08-13 | 2026-08-13T09:50:01Z | **PASS** | 0 | 0 | 0 | 0 | skipped | — |
| 2026-08-14 | 2026-08-14T09:50:01Z | **PASS** | 12 | 12 | 0 | 0 | skipped | — |
| 2026-08-15 | 2026-08-15T09:50:01Z | **PASS** | 72 | 72 | 0 | 0 | skipped | — |
| 2026-08-16 | 2026-08-16T09:50:01Z | **PASS** | 26 | 26 | 0 | 0 | skipped | — |
| 2026-08-17 | 2026-08-17T09:50:01Z | **PASS** | 26 | 26 | 0 | 0 | skipped | — |
| 2026-08-18 | 2026-08-18T09:50:02Z | **PASS** | 26 | 26 | 0 | 0 | skipped | — |
| 2026-08-19 | 2026-08-19T09:50:01Z | **PASS** | 18 | 18 | 0 | 0 | skipped | — |
| 2026-08-20 | 2026-08-20T09:50:01Z | **PASS** | 21 | 21 | 0 | 0 | skipped | — |
| 2026-08-21 | 2026-08-21T09:50:01Z | **PASS** | 26 | 26 | 0 | 0 | skipped | — |
| 2026-08-22 | 2026-08-22T09:50:01Z | **PASS** | 0 | 0 | 0 | 0 | skipped | — |
| 2026-08-23 | 2026-08-23T09:50:01Z | **PASS** | 13 | 12 | 0 | 0 | skipped | — |
| 2026-08-24 | 2026-08-24T09:50:01Z | **PASS** | 9 | 9 | 0 | 0 | skipped | — |
| 2026-08-25 | 2026-08-25T09:50:01Z | **PASS** | 55 | 53 | 0 | 0 | skipped | — |
| 2026-08-26 | 2026-08-26T09:50:01Z | **PASS** | 13 | 13 | 0 | 0 | skipped | — |
| 2026-08-27 | 2026-08-27T09:50:01Z | **PASS** | 7 | 7 | 0 | 0 | skipped | — |
| 2026-08-28 | 2026-08-28T09:50:02Z | **PASS** | 8 | 8 | 0 | 0 | skipped | — |
| 2026-08-29 | 2026-08-29T09:50:01Z | **PASS** | 8 | 8 | 0 | 0 | skipped | — |
| 2026-08-30 | 2026-08-30T09:50:01Z | **PASS** | 45 | 45 | 0 | 0 | skipped | — |
| 2026-08-31 | 2026-08-31T09:50:01Z | **PASS** | 77 | 77 | 0 | 0 | skipped | — |
| 2026-09-01 | 2026-09-01T09:50:01Z | **PASS** | 77 | 77 | 0 | 0 | skipped | — |
| 2026-09-02 | 2026-09-02T09:50:01Z | **PASS** | 84 | 84 | 0 | 0 | skipped | — |
| 2026-09-03 | 2026-09-03T09:50:01Z | **PASS** | 92 | 92 | 0 | 0 | skipped | — |
| 2026-09-04 | 2026-09-04T09:50:01Z | **PASS** | 94 | 92 | 0 | 0 | skipped | — |
| 2026-09-05 | 2026-09-05T09:50:01Z | **PASS** | 2 | 2 | 0 | 0 | skipped | — |
| 2026-09-06 | 2026-09-06T09:50:01Z | **PASS** | 5 | 5 | 0 | 0 | skipped | — |
| 2026-09-07 | 2026-09-07T09:50:01Z | **PASS** | 5 | 5 | 0 | 0 | skipped | — |
| 2026-09-08 | 2026-09-08T09:50:01Z | **PASS** | 5 | 5 | 0 | 0 | skipped | — |
| 2026-09-09 | 2026-09-09T09:50:01Z | **PASS** | 5 | 5 | 0 | 0 | skipped | — |

## Status

**Instrumentation:** keep on (registry, invariant, BFF/Django logs, `/system/status`).  
**Phase 2a:** not blocked by this log — blocked only by an **active** unresolved leak/saturation or missing instrumentation.

### Ops notes

- Daily cron (host): `50 9 * * * /opt/stacks/stay.hr/scripts/observe-sse-lifecycle-cron.sh` → `/var/log/sse-lifecycle-observe.log`
- Token: set `RECEPTION_API_TOKEN` in `/etc/stay/sse-observe.env` (preferred) or keep load-test creds available for the cron wrapper
- Canary wait is ~70s (two SSE heartbeats); use `--canary` when verifying rise/fall explicitly
- Cross-worker totals: `data/media/ops/daily_ops_report/docker_signals.json` (`sse_invariant_breach` is **CRIT** in daily ops)
