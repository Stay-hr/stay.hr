# AGENTS.md — stay.hr

Guidance for AI agents and developers working in this repository.

## Current mode: production

**Frontends are running Next.js production builds** (`stay-hr-web-*:latest`, `node server.js`). Dev override is disabled (`docker-compose.override.yml.bak`).

To return to development mode, see [README.md — Frontend: production vs development mode](README.md#frontend-production-vs-development-mode).

### What this means

| Layer | Mode | Hot reload |
|-------|------|------------|
| `web/booking` | `next dev` (Docker) | Yes — edit TSX/CSS, refresh browser |
| `web/reception` | `next dev` (Docker) | Yes |
| `django` / Celery | Production image | No — restart container or `./scripts/deploy.sh` after Python changes |

Dev mode is enabled by [`docker-compose.override.yml`](docker-compose.override.yml), which Docker Compose loads automatically alongside [`docker-compose.yml`](docker-compose.yml).

### Frontend workflow

1. Edit files under `web/booking/` or `web/reception/`
2. Save — Next.js recompiles inside the container
3. Refresh the browser (Traefik URLs unchanged: `booking.uzorita.hr`, `app.stay.hr`, etc.)

**Do not** run `docker compose build web-booking` or `web-reception` for routine UI work.

After changing `package.json` or adding npm dependencies:

```bash
docker compose build web-booking web-reception
docker compose up -d web-booking web-reception
```

### Backend workflow

Python/API changes still require a Django container restart or deploy:

```bash
./scripts/deploy.sh
# or
docker compose restart django
```

Remote deploy from Windows (after `git push`): `.\scripts\remote-deploy.ps1` — see [domain-setup.md](docs/operations/domain-setup.md#deploy-s-githuba).

### Switch to production frontends

Remove or rename `docker-compose.override.yml`, then rebuild and start:

```bash
mv docker-compose.override.yml docker-compose.override.yml.bak
docker compose build web-booking web-reception
docker compose up -d web-booking web-reception
```

### Switch back to development mode

Restore the override file, then rebuild and start:

```bash
mv docker-compose.override.yml.bak docker-compose.override.yml
docker compose build web-booking web-reception
docker compose up -d web-booking web-reception
```

### Local (non-Docker) frontend dev

```bash
cd web/booking   # or web/reception
npm install
npm run dev
```

Set `STAY_API_INTERNAL_URL` and `STAY_BOOKING_API_TOKEN` in `.env` / shell. See [web/README.md](web/README.md).

### Overbooking / multi-room (Uzorita)

- Runbook: [`docs/operations/multi-room-overbooking-checklist.md`](docs/operations/multi-room-overbooking-checklist.md)
- Reconcile: `python manage.py reconcile_booking_units --tenant-id 2`
- Daily scan: Celery `detect_overbooking_daily` (06:00, tenant 2)

### Key paths

- Booking app: `web/booking/`
- Reception app: `web/reception/`
- Django API: `backend/`
- Ops runbook: `docs/operations/domain-setup.md`
- Property financial report: [`docs/development/property-financial-report.md`](docs/development/property-financial-report.md) — v1 JSON contract (frozen); ADR [0003](docs/architecture/adr/0003-property-financial-report.md). Exporters under `backend/apps/reservations/reports/exports/` must consume `PropertyFinancialReportResult` only — no ORM or re-aggregation in export code.

### Document intake — tenant invariant

Cross-tenant WhatsApp is intentional: inbound `WhatsAppMessage` stays on the WABA tenant (`platform`); the reservation lives on the property tenant (`demo`, etc.). Intake entities (job, session, image, draft/outbound scoped to a reservation) must use **`reservation.tenant_id`**.

**Invariant:** when `DocumentIntakeJob.reservation_id` is set, `job.tenant_id == Reservation.tenant_id`.

**Pipeline rule (code review):** match/apply/guest lookup must use `ctx.effective_tenant_id` from `DocumentIntakeContext.from_job(job)` — never `job.tenant_id` in pipeline logic.

| Allowed `job.tenant_id` | Forbidden in match/apply |
|-------------------------|---------------------------|
| `DocumentIntakeContext.from_job` (legacy heal) | `match_persons_to_guests(... tenant_id=job.tenant_id ...)` |
| Create path before `reservation_id` exists | `Reservation.objects.filter(... tenant_id=job.tenant_id ...)` in intake layer |
| Storage path (`document_intake_image_upload_to`) | |

All pipeline functions take `ctx: DocumentIntakeContext` (frozen). Celery tasks, API views, and reconcile build `ctx` at the system boundary.

Runbook: [`docs/operations/ocr-multi-guest-rules.md`](docs/operations/ocr-multi-guest-rules.md)

Telemetry (OCR-D, write-only): [`docs/development/document-intake-telemetry.md`](docs/development/document-intake-telemetry.md). Weekly KPIs: `python manage.py document_intake_quality_report --days 7`. Do not gate apply/finalize on `quality_score` until baseline exists.

**Daily OCR email report:** Celery `send_document_intake_quality_report` at 09:00 Europe/Zagreb. Env: `DOCUMENT_INTAKE_QUALITY_REPORT_*` (see `.env.example`). Snapshot: `data/media/ops/document_intake_report_snapshot.json`. After `.env` changes: `docker compose up -d django celery-worker celery-beat`.

### Guest web check-in (document channel)

**Guest document intake is web-only** (`GUEST_CHECKIN_WEB_ONLY=true`): `booking.{tenant}/check-in/{token}` — manual PATCH + `WEB_GUEST` OCR (`POST .../documents/`, `GET .../jobs/{id}/`). WhatsApp Auto check-in and inbound guest images send the web link only; no guest `DocumentIntakeJob(source=whatsapp)`. Staff Operator Toni / Hospira batch OCR unchanged. ADR: [0004](docs/architecture/adr/0004-guest-checkin-session.md).

## Gunicorn + SSE (Reception)

**Thesis:** event **distribution** (Redis EventBus) is separate from **transport** (Gunicorn vs dedicated Uvicorn SSE). Full Django ASGI is optional (Phase 2c), not required for SSE scaling. See [ADR 0005](docs/architecture/adr/0005-gunicorn-sse-worker-evolution.md).

- Gunicorn env: `GUNICORN_*` in `.env` — launcher `scripts/run-gunicorn.sh` (not entrypoint). After `.env` change: `docker compose up -d django` (no rebuild unless Dockerfile/script changed).
- Status: `GET /api/v1/reception/system/status/` — **reception:read**; `metrics_scope=worker_process` (not global). Fields: `build.git_sha`, `build.started_at`, `build.hostname`; `messaging` inventory/outbox (ADR 0010). Post-v1 work: [Rollout & Adoption](docs/operations/messaging-orchestration-post-v1.md) — no Phase 9 core.
- Benchmark: `./scripts/benchmark-health-latency.sh` — p50/p95/p99 (`BENCHMARK_LIGHT=1` for CI; `OPS_CI_ARTIFACT_DIR` for artifacts).
- Load test before sign-off: `./scripts/load-test-gunicorn-sse.sh` (`LOAD_TEST_LIGHT=1` for CI; requires `RECEPTION_API_TOKEN`, `LOAD_TEST_RESERVATION_ID`).
- **Monitoring checklist:** [gunicorn-sse-monitoring.md](docs/operations/gunicorn-sse-monitoring.md). ADR: [0005](docs/architecture/adr/0005-gunicorn-sse-worker-evolution.md).
- **Lifecycle instrumentation (permanent):** `stream_id` registry, `invariant_delta` / `sse_invariant_breach`, BFF AbortSignal logs (`bff_sse_*`), Django `sse_stream_opened`/`closed`, and `GET /system/status` SSE block stay **on through Phase 2a (Redis) and 2b (Uvicorn)** — do not strip when changing the event bus or transport. Daily observe remains useful: `./scripts/observe-sse-lifecycle.sh --append-log` → [sse-lifecycle-observation-log.md](docs/operations/sse-lifecycle-observation-log.md). **No calendar “N days PASS” gate** blocks Redis; if leak/saturation returns → stop and analyze with these tools; if it does not recur → continue Phase 2a.
- **Postmortem:** [2026-07-08 SSE worker exhaustion](docs/operations/incidents/2026-07-08-sse-worker-exhaustion.md).
- One-off `manage.py` / tests: use **`django-run`** profile (`docker compose --profile test-run run --rm django-run …`) — no Traefik labels.
- **Flags:** `RESERVATION_VERSION_EVENT_BUS=in_process|redis` (Phase 2a; default `in_process` in `base.py`). Rollback: set `in_process`, then `docker compose up -d django celery-worker`. Phase 2b stub only: `SSE_TRANSPORT=gunicorn|uvicorn`.
- Phase 2a (Redis EventBus) → Phase 2b (dedicated Uvicorn SSE) if ADR triggers met — see ADR phase 2 trigger criteria. Instrumentation above is a prerequisite for both phases, not a temporary observation-only feature.

## Daily ops report

- Celery task: `core.send_daily_ops_report` at 10:00 Europe/Zagreb
- Env: `DAILY_OPS_REPORT_*` (see `.env.example`); no ops API token — collectors run in-process
- Snapshot: `{MEDIA_ROOT}/ops/daily_ops_report/snapshot.json`; Markdown: `latest.md` + `YYYY-MM-DD.md`
- After `.env` changes: `docker compose up -d django celery-worker celery-beat`
- CLI: `python manage.py daily_ops_report` (`--print-markdown`, `--json`, `--no-email`, `--write-only`)
- Experimental host helper: `./scripts/collect-docker-gunicorn-signals.sh` → `docker_signals.json`

## FCM push deployment

Runbook: [docs/operations/fcm-push-guard.md](docs/operations/fcm-push-guard.md)

Any deployment that changes FCM configuration or notification delivery must verify:

- `FCM_PUSH_ALLOWED_TENANT_SLUGS` is configured in the target `.env`.
- After any `.env` change, recreate containers with `docker compose up -d django celery-worker` (do not use `restart` — it reuses stale env).
- A smoke test is performed against the `demo` tenant before opening the system to users.
- Verify logs do not contain `reason=allowlist_empty`.

### Maintenance

Before deploy, import, or seed: set `FCM_PUSH_MAINTENANCE=true` in `.env`, then `docker compose up -d django celery-worker`.

After the operation: set `FCM_PUSH_MAINTENANCE=false`, then `docker compose up -d django celery-worker` again.

Do not use allowlist edits for maintenance. During maintenance, logs should show `reason=maintenance_mode`, not `tenant_not_allowed`.

## Backend testing

Backend testovi idu na **dedicated PostGIS bazu**, ne na produkcijsku `stay_platform_db` i ne na SQLite.

| | |
|--|--|
| **PostGIS container** | `postgis` (mreža `postgis`) |
| **Test baza** | `stay_platform_test_db` (owner `stay`) |
| **Settings** | `DJANGO_SETTINGS_MODULE=config.settings.test_postgis` |
| **Referenca** | PostGIS za `apps.integrations.tests` — ne SQLite |
| **Detalji** | [test-suite.md](docs/development/test-suite.md), [integrations-test-triage.md](docs/development/integrations-test-triage.md) |

Kod **nije** bind-mountan u `stay_django` — nakon izmjena u `backend/` uvijek rebuild prije testova:

```bash
docker compose build django
```

### Brzi start (recommended workflow)

```bash
./scripts/ensure-test-db.sh
docker compose build django
./scripts/run-tests-postgis.sh          # default: apps.integrations.tests (CI smoke)
```

Proizvoljni moduli:

```bash
./scripts/run-tests-postgis.sh apps.tenants.tests apps.integrations.tests -v 2
```

Uži eVisitor smoke:

```bash
./scripts/run-tests-postgis.sh \
  apps.integrations.tests.test_smoke_evisitor \
  apps.integrations.tests.test_seed_evisitor_config \
  -v 2
```

Ručno:

```bash
docker compose --profile test-run run --rm \
  -e DJANGO_SETTINGS_MODULE=config.settings.test_postgis \
  -e TEST_DB_NAME=stay_platform_test_db \
  django-run python manage.py test apps.tenants.tests.test_seed_demo_guest --keepdb -v 2
```

**Produkcija:** ne koristiti `docker compose run django` — one-off kontejner nasljeđuje Traefik labele s `django` servisa i može uzrokovati intermittentne 502. Za testove i `manage.py` one-shot koristiti **`django-run`** (profil `test-run`, bez proxy mreže).

Za čistu test bazu: `docker exec postgis psql -U postgres -c 'DROP DATABASE stay_platform_test_db;'`, zatim `./scripts/ensure-test-db.sh`.

Integration suite: **335/335** na PostGIS-u (stabilizacija 2026-07). Povijest burn-downa: [integrations-test-triage.md](docs/development/integrations-test-triage.md). Novi PR-ovi moraju zadržati zeleni smoke — ne sužavati suite.

### Pre-merge checklist (backend, integracije)

Svaki backend PR koji dira `backend/apps/integrations/`, eVisitor, Channex, WhatsApp ili povezane API/view slojeve:

```bash
./scripts/ensure-test-db.sh
docker compose build django
./scripts/run-tests-postgis.sh
```

Ako PR dira eVisitor bootstrap ili deployment:

```bash
./scripts/verify-demo-evisitor.sh
```

Cilj: **335/335** prije mergea. Ne sužavati default smoke da bi testovi prošli.

### Načela razvoja (nakon stabilizacije 2026-07)

Fokus je na poslovnim funkcionalnostima uz očuvanje kvalitete platforme:

- **Jedan PR = jedna odgovornost** (kao burn-down PR-A … PR-F)
- **Dokumentacija ide uz funkcionalnost** (ne odvojeni docs-only PR osim ako je namjerno)
- **Testovi dolaze s implementacijom** — PostGIS integration smoke za integracijski sloj
- **Bez usputnih refaktora** osim ako su nužni za taj PR

Referenca stabilizacije: [test-suite.md — Integration Suite Stabilization (2026-07)](docs/development/test-suite.md#integration-suite-stabilization-2026-07).

### Reservation versioning

**Novi modul koji uvodi stanje rezervacije** mora koristiti `ReservationVersion` (`touch_reservation_version`, `sync-versions` + `scope`) i **ne smije** uvoditi vlastiti mehanizam pollinga ili verzioniranja bez odobrene arhitekturne odluke (ADR).

Zabranjeno bez ADR-a: paralelni modeli (`PaymentVersion`, `HousekeepingVersion`, …), vlastiti poll hookovi, `COUNT`/`MAX` na svakom zahtjevu umjesto event-driven bumpa.

Referenca: [reservation-versioning.md](docs/architecture/reservation-versioning.md), ADR [0001-reservation-event-versioning](docs/architecture/adr/0001-reservation-event-versioning.md).
