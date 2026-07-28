# Test suite — stay.hr backend

## Integration Suite Stabilization (2026-07)

Referentna točka nakon sustavnog burn-downa integration testova:

| Metrika | Početno (triage) | Završno |
|---------|-----------------:|--------:|
| Passed | 315 | **335** |
| Failed | 17 | **0** |
| Errors | 3 | **0** |

Što je uključeno:

- Migracija na PostGIS-based integration testing (`test_postgis`, `stay_platform_test_db`)
- eVisitor bootstrap, smoke i deploy verifikacija (`seed_evisitor_config`, `smoke_evisitor`, `verify-demo-evisitor`)
- Stabilizacija cijelog `apps.integrations.tests` suitea (PR-A … PR-E, test-only)
- **Rezultat: 335 / 335 passing**

Povijest popravaka i PR po PR: [integrations-test-triage.md](integrations-test-triage.md).

**Quality gate:** svaka nova funkcionalnost u integracijskom sloju mora zadržati **335/335** na PostGIS smoke-u (`./scripts/run-tests-postgis.sh`).

### GitHub Actions — ePorezna

Workflow [`.github/workflows/eporezna-tests.yml`](../../.github/workflows/eporezna-tests.yml) na `push`/`pull_request` (path-filter) pokreće:

```text
apps.billing.tests.test_eporezna_pdv
apps.billing.tests.test_eporezna_pdvs
apps.api.tests.test_reception_eporezna
```

protiv PostGIS service containera (`config.settings.test_postgis`). Ručno: **Actions → ePorezna tests → Run workflow**.

---

## Recommended workflow

Copy/paste za lokalni razvoj i CI smoke na stvarnom PostGIS-u (`stay_platform_test_db`):

```bash
cd /opt/stacks/stay.hr
./scripts/ensure-test-db.sh
docker compose build django
./scripts/run-tests-postgis.sh
```

Bez argumenata, `run-tests-postgis.sh` pokreće **`apps.integrations.tests`** (uključuje eVisitor, Channex, WhatsApp i sve buduće integration testove). Koraci 1 i 2 skripta ponavlja interno ako ih preskočiš — gore navedeni redoslijed je eksplicitan za nove developere.

Uži smoke (samo eVisitor seed + smoke naredbe):

```bash
./scripts/run-tests-postgis.sh \
  apps.integrations.tests.test_smoke_evisitor \
  apps.integrations.tests.test_seed_evisitor_config \
  -v 2
```

Puni backend suite:

```bash
./scripts/run-tests-postgis.sh \
  apps.api.tests \
  apps.core.tests \
  apps.tenants.tests \
  apps.integrations.tests \
  apps.properties.tests \
  apps.reservations.tests \
  apps.legacy_import.tests \
  -v 2
```

Settings: `DJANGO_SETTINGS_MODULE=config.settings.test_postgis`. Detalji u [AGENTS.md](../../AGENTS.md#backend-testing).

**FCM push guard:** test settings (`test.py`, `test_postgis.py`) postavljaju `FCM_PUSH_ENABLED=false`, pa se push ne šalje tijekom testova bez obzira na Firebase credentials u kontejneru. Za ručne operacije na serveru (import, seed, reconcile) privremeno postavi `FCM_PUSH_MAINTENANCE=true` u `.env`, zatim `docker compose up -d django celery-worker` (ne `restart` — env se ne osvježava). Operativni runbook: [fcm-push-guard.md](../operations/fcm-push-guard.md).

**Triage (burn-down):** [integrations-test-triage.md](integrations-test-triage.md) — **335/335**, **0 errors** (stabilizacija završena 2026-07).

---

Zadnji puni run: **2026-05-26** (Docker image `stay-hr-django:latest`, nakon uklanjanja Smoobu integracije).

## Pokretanje

### PostGIS (preporučeno)

Vidi [Recommended workflow](#recommended-workflow) iznad. Test baza: `stay_platform_test_db` na kontejneru `postgis`.

### Puni suite (ručno, bez skripte)

```bash
cd /opt/stacks/stay.hr
docker compose build django
docker compose --profile test-run run --rm \
  -e DJANGO_SETTINGS_MODULE=config.settings.test_postgis \
  -e TEST_DB_NAME=stay_platform_test_db \
  django-run python manage.py test \
  apps.api.tests \
  apps.core.tests \
  apps.tenants.tests \
  apps.integrations.tests \
  apps.properties.tests \
  apps.reservations.tests \
  apps.legacy_import.tests \
  -v 2 --keepdb
```

### Default `manage.py test` (djelomično)

```bash
docker compose --profile test-run run --rm django-run python manage.py test
```

Pronalazi samo **~53 testa** (uglavnom `api`, `core`, `tenants`). Ne uključuje automatski `integrations`, `properties`, `reservations`, `legacy_import` — koristi eksplicitne labele iznad.

### Integracije (Channex, eVisitor, …)

```bash
./scripts/run-tests-postgis.sh apps.integrations.tests -v 2
```

Ili bez skripte (legacy):

```bash
docker compose --profile test-run run --rm django-run python manage.py test apps.integrations.tests -v 2
```

### Channex cert (plan ispravki)

```bash
docker compose run --rm django python manage.py test \
  apps.integrations.tests.test_channex_ari \
  apps.integrations.tests.test_channex_booking_ingest \
  -v 2
```

---

## Zadnji rezultat (2026-05-20)

| Metrika | Vrijednost |
|---------|------------|
| **Ukupno** | 121 test |
| **Prošlo** | **121** |
| **Palo** | 0 |
| **Trajanje** | ~7–8 s |
| **Status** | **OK** |

### Po aplikaciji

| Modul | Prošlo | Napomena |
|-------|--------|----------|
| `apps.api.tests` | 15/15 | reception, rooms, FCM, unit serializers |
| `apps.core.tests` | 7/7 | notifications, firebase |
| `apps.tenants.tests` | 21/21 | admin scope, API apps, token encryption |
| `apps.integrations.tests` | Channex, channel manager, evisitor, webhook |
| `apps.properties.tests` | 19/19 | units, beds, bathrooms, seeds |
| `apps.reservations.tests` | 6/6 | models, statistics, notifications |
| `apps.legacy_import.tests` | 5/5 | validator bez live legacy DB |

---

## Integracije — što je pokriveno

### Channex (cert tenant `demo`)

| Datoteka | Testovi | Svrha |
|----------|---------|--------|
| `test_channex_ari.py` | 8 | full sync restrictions, **delta rate push**, outbox flush |
| `test_channex_booking_ingest.py` | 7 | webhook ingest, ack, idempotency, **revisions feed** |
| `test_channex_webhook.py` | 3 | auth header, payload |
| `test_channex_mapping.py` | 2 | room type mapping |

### Channel manager / Channex ARI

| Datoteka | Svrha |
|----------|--------|
| `test_channel_dispatch.py` | outbound sync, calendar blocks |
| `test_channel_manager_resolver.py` | `none` / `channex` validation |
| `test_channex_ari.py` | full sync, delta rate push, outbox |
| `test_channex_reservation_availability.py` | availability push |

---

## Legacy import testovi

`ValidatorTests` koristi `@override_settings(DATABASES=…)` **bez** `uzorita_legacy`, jer unit testovi provjeravaju logiku kad legacy baza nije dostupna (isti scenarij kao lokalni/CI test bez `UZORITA_DB_*`).

Za validator protiv stvarne Uzorita baze pokreni management command na serveru s postavljenim `UZORITA_DB_NAME`, ne Django unit test.

---

## Preduvjeti

- Docker Compose stack (`postgis` mreža, env iz `.env`)
- Test baza `stay_platform_test_db` — `./scripts/ensure-test-db.sh`
- Image se gradi iz repoa (kod **nije** bind-mountan u `stay_django` — nakon izmjena uvijek `docker compose build django`)

## Povezana dokumentacija

- Channex cert: [channex-demo-property.md](../integrations/channex-demo-property.md)
- Channel manager: [channel-manager-setup.md](../operations/channel-manager-setup.md)
