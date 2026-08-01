# Operacije Stay.hr

Operativne upute: **domene** (booking + recepcija web), **deploy** s GitHuba, **Hospira** reception API (sync-versions).

**Repo na serveru:** `/opt/stacks/stay.hr`  
**SSH host (produkcija):** `dedicated-hel1` (alias u `~/.ssh/config`, `65.108.196.92`)  
**Deploy:** `git pull` + `./scripts/deploy.sh` (vidi [Deploy s GitHuba](#deploy-s-githuba))

---

## Tri sloja + internal mreža

| Sloj | Tko konfigurira | Što radi |
|------|-----------------|----------|
| **Cloudflare DNS** | Django backend (admin / management command) | A/CNAME zapisi, proxied → Hetzner IP |
| **Traefik** | `docker-compose.yml` labele (jednokratno + custom hostovi) | TLS (ACME DNS-01) + routing po `Host` |
| **Django `TenantDomain`** | Admin (`admin.stay.hr`) | Koji `Host` = koji tenant + property (booking kontekst) |
| **Docker `stay_internal`** | `docker-compose.yml` | Backend-to-backend (`stay_django:8000`) bez hairpin NAT-a |

Token **`CF_DNS_API_TOKEN`** — isti kao u `/opt/stacks/traefik/.env` (Traefik cert resolver + Django DNS upsert).

### Docker mreže

```mermaid
flowchart TB
  Internet --> CF[Cloudflare_DNS]
  CF --> Traefik
  subgraph proxyNet [proxy_external]
    Traefik
    WebBooking[stay_web_booking]
    WebReception[stay_web_reception]
    DjangoEdge[stay_django]
  end
  subgraph stayInternal [stay_internal]
    WebBooking
    WebReception
    DjangoEdge
    CeleryWorker
    CeleryBeat
  end
  Traefik --> WebBooking
  Traefik --> WebReception
  Traefik --> DjangoEdge
  WebBooking -->|"http://stay_django:8000 + Host header"| DjangoEdge
  WebReception -->|"http://stay_django:8000 + Host header"| DjangoEdge
```

| Servis | `proxy` | `stay_internal` | `postgis` | `hetzner_net` |
|--------|---------|-----------------|-----------|---------------|
| `django` | da | da | da | da |
| `celery-worker` | ne | da | da | da |
| `celery-beat` | ne | da | da | da |
| `web-booking` | da | da | — | — |
| `web-reception` | da | da | — | — |

Web SSR/API proxy mora **prosljeđivati originalni `Host`** (npr. `booking.uzorita.hr`) da `TenantHostMiddleware` i `site-context` rade ispravno.

---

## Preduvjeti

1. Traefik stack na `/opt/stacks/traefik`, mreža `proxy`
2. Mreža `stay_internal` (compose je kreira automatski; ili `docker network create stay_internal`)
3. U Django `.env` na serveru ( `/opt/stacks/stay.hr/.env` ):

```env
CF_DNS_API_TOKEN=...          # Cloudflare API token (Zone DNS Edit)
STAY_SERVER_IP=...            # Javni IP Hetzner servera (proxied A record)
CLOUDFLARE_ZONE_STAY=stay.hr  # Zona za *.stay.hr subdomene
STAY_API_INTERNAL_URL=http://stay_django:8000
STAY_PUBLIC_API_URL=https://api.stay.hr
```

4. Web frontend containeri (`web/booking`, `web/reception`) deployani s Traefik labelama

Za legacy bootstrap `api.stay.hr` / `admin.stay.hr` i dalje je dostupna skripta `./scripts/cloudflare_dns_upsert.sh`.

---

## A) Jednokratno — platforma

Prije go-live web frontenda (recepcija + booking subdomene).

### 1. DNS (backend)

```bash
cd /opt/stacks/stay.hr
docker compose run --rm django python manage.py provision_platform_dns
```

Očekivani zapisi u zoni `stay.hr` (proxied A → `STAY_SERVER_IP`):

| Host | Namjena |
|------|---------|
| `app.stay.hr` | Recepcija u browseru |
| `*.stay.hr` | Wildcard — tenant booking subdomene (`uzorita.stay.hr`, …) |

API i admin (`api.stay.hr`, `admin.stay.hr`) već postoje — ne dirati osim provjere.

### 2. Traefik (docker-compose)

U `docker-compose.yml` web servisa — jednokratno (već u repou):

- `Host(\`app.stay.hr\`)` → `stay-web-reception` (prioritet 100)
- `HostRegexp(\`^[a-z0-9-]+\\.stay\\.hr$\`)` → `stay-web-booking` (prioritet 50)
- `tls.certresolver=cloudflare` na routerima
- Custom domene: eksplicitni `Host()` routeri (npr. `booking.uzorita.hr`)

Zatim:

```bash
docker compose up -d
```

### 3. Provjera

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://app.stay.hr/
curl -sS -o /dev/null -w '%{http_code}\n' https://uzorita.stay.hr/
```

Očekivano: `200` ili `307` (redirect), ne `404`/`502`.

---

## B) Nova Stay subdomena (`*.stay.hr`)

Primjer: `uzorita-sibenik.stay.hr` (primary booking za tenant `uzorita`) ili `demo.stay.hr`.

Wildcard DNS (`*.stay.hr`) i Traefik `HostRegexp` pokrivaju **routing**. Za pouzdan **TLS cert** (Let’s Encrypt DNS-01) dodaj eksplicitni `Host()` router — `HostRegexp` sam ne izdaje cert po pojedinačnom hostu.

### Koraci

1. **Admin** → [Tenant domains](https://admin.stay.hr/admin/tenants/tenantdomain/) → **Add**:
   - `domain`: `uzorita-sibenik.stay.hr` (ili `demo.stay.hr`)
   - `tenant`: uzorita (ili demo)
   - `property`: FK na objekt ako booking ide direktno na taj objekt (inače ostavi prazno = tenant hub)
   - `domain_type`: `stay_subdomain`
   - `is_primary`: `True` ako ovo treba biti kanonska domena za guest check-in linkove
   - `is_verified`: `False`

2. Odaberi red → admin akcija **Provision DNS** → backend upsert-a A zapis u Cloudflare zoni `stay.hr`  
   (`CF_DNS_API_TOKEN` mora biti isti valjani Zone DNS Edit token kao u `/opt/stacks/traefik/.env`).

3. **Traefik** — eksplicitni router u `docker-compose.yml` (`web-booking`) za TLS:

```yaml
- traefik.http.routers.stay-booking-<slug>.rule=Host(`uzorita-sibenik.stay.hr`)
- traefik.http.routers.stay-booking-<slug>.entrypoints=websecure
- traefik.http.routers.stay-booking-<slug>.tls=true
- traefik.http.routers.stay-booking-<slug>.tls.certresolver=cloudflare
- traefik.http.routers.stay-booking-<slug>.service=stay-web-booking
- traefik.http.routers.stay-booking-<slug>.priority=100
```

```bash
cd /opt/stacks/stay.hr
docker compose up -d web-booking
```

4. Provjera:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://uzorita-sibenik.stay.hr/
curl -sS -H "Host: uzorita-sibenik.stay.hr" http://stay_django:8000/api/v1/public/site-context/
# TLS SAN mora biti taj host (ne TRAEFIK DEFAULT CERT):
echo | openssl s_client -connect 127.0.0.1:443 -servername uzorita-sibenik.stay.hr 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

5. `is_verified=True` u adminu (guest check-in base URL bira verified + primary property domain).

### Slug u URL-u (više objekata po tenantu)

Ako `TenantDomain.property` **nije** postavljen (tenant hub):

- `https://demo.stay.hr/p/<property-slug>/` — booking za taj objekt
- Primjer hub: `https://uzorita.stay.hr/p/uzorita/`
- Primjer primary property booking: `https://uzorita-sibenik.stay.hr/`
---

## C) Custom domena (vanjski host)

Primjer: `booking.uzorita.hr` → tenant `uzorita`, property `uzorita`.

Wildcard `*.stay.hr` **ne pokriva** vanjsku domenu — treba i DNS i Traefik `Host()` rule.

### Koraci

1. **Admin** → TenantDomain → **Add**:
   - `domain`: `booking.uzorita.hr`
   - `tenant`: uzorita
   - `property`: Uzorita Luxury Rooms (`slug=uzorita`)
   - `domain_type`: `custom_domain`
   - `is_verified`: `False`

2. Admin akcija **Provision DNS**  
   Backend upsert-a zapis u zoni `uzorita.hr` (mora biti u istom Cloudflare accountu kao token).

3. **Traefik** — label u `docker-compose.yml` booking servisa (Uzorita MVP):

```yaml
- traefik.http.routers.stay-booking-uzorita.rule=Host(`booking.uzorita.hr`)
- traefik.http.routers.stay-booking-uzorita.entrypoints=websecure
- traefik.http.routers.stay-booking-uzorita.tls=true
- traefik.http.routers.stay-booking-uzorita.tls.certresolver=cloudflare
- traefik.http.routers.stay-booking-uzorita.service=stay-web-booking
```

```bash
cd /opt/stacks/stay.hr
docker compose up -d
```

4. Provjera:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://booking.uzorita.hr/
curl -sS -H "Host: booking.uzorita.hr" http://stay_django:8000/api/v1/public/site-context/
```

5. `is_verified=True` u adminu

---

## D) Recepcija (`app.stay.hr`)

- Fiksna platform domena — **ne ovisi** o property slug-u
- Nema javnog bookiranja
- **Web recepcija** (`app.stay.hr/login`): Django staff login (korisničko ime/email + lozinka). Korisnik mora imati `is_staff=True` i `TenantMembership` za tenant. Ako pripada više tenantima, login traži odabir tenanta.
- **Hospira tablet**: device token (ApiApplication Bearer) — ne mijenja se
- DNS: uključen u `provision_platform_dns`
- Traefik: `Host(\`app.stay.hr\`)` → reception container
- **ApiApplication scopes** za Hospiru/tablet: `reception:read`, `reception:write`, `public:read`. **Ne** dodavati `admin:read` / `admin:write` — Django ih blokira na mobilnim endpointima (`DenyAdminScopes`).
- Ako tablet token ne radi nakon regeneracije u adminu, kopiraj novi **Device token** s change forme (admin akcija „Regenerate API token” poništava stari bearer).

---

## E) Uzorita go-live (automatski rollout)

Management command za seed + DNS + provjere:

```bash
cd /opt/stacks/stay.hr
docker compose run --rm django python manage.py rollout_uzorita_domains
```

Command:

1. Upsert-a `TenantDomain` zapise:
   - `uzorita-sibenik.stay.hr` (property booking, **primary**)
   - `uzorita.stay.hr` (tenant hub)
   - `booking.uzorita.hr` (legacy custom; DNS se ne provisionira ako zona ne postoji)
2. Pokreće `provision_platform_dns` + **Provision DNS** za `*.stay.hr` domene
3. Curl provjere (javni hostovi + internal `site-context`)
4. Postavlja `is_verified=True` ako sve prođe

Opcije: `--skip-dns`, `--skip-verify`, `--dry-run`.

Primarna javna booking adresa (2026-08): **`https://uzorita-sibenik.stay.hr/`** — zamjena za `booking.uzorita.hr` dok vanjska domena nije dostupna.

---

## Deploy s GitHuba

**Preduvjet:** commit mora biti na `origin` (`git push`) prije remote deploya — inače server `git pull` ne povlači ništa novo.

### Windows (iz lokalnog repoa)

Nakon lokalnog `git pull` / `git push`:

```powershell
cd stay.hr
.\scripts\remote-deploy.ps1
```

Opcije: `-DryRun` (ispiše SSH naredbu), `-Branch main`, `-SshHost dedicated-hel1`, `-RemotePath /opt/stacks/stay.hr`.

Skripta preko SSH na `dedicated-hel1` pokreće `git pull` i `./scripts/deploy.sh` u `/opt/stacks/stay.hr`.

### Ručno na serveru (SSH)

```bash
ssh dedicated-hel1
cd /opt/stacks/stay.hr
git pull
./scripts/deploy.sh
```

`deploy.sh` rebuild-a image ako:

- ima novih migracija (datoteke ili neprimijenjenih u bazi), ili
- se **backend Python kod** promijenio od zadnjeg builda (`backend/**/*.py`, `requirements.txt`, `Dockerfile`)

Inače samo `docker compose restart`. Na kraju pokreće `python manage.py check`.

Ako je u `.env` postavljen `DEMO_EVISITOR_USERNAME`, deploy zatim pokreće [`scripts/verify-demo-evisitor.sh`](../../scripts/verify-demo-evisitor.sh): `seed_evisitor_config` + `smoke_evisitor --login-only --json`. Bez tih varijabli korak se preskače.

**Važno:** samo `docker compose restart` **ne** deploya code-only promjene — kod je u Docker imageu. Očekuj `Rebuild required (backend source changed since last image build)` nakon pull-a s API izmjenama.

**Ručni fallback** ako rebuild nije pokrenut:

```bash
docker compose build django celery-worker celery-beat
docker compose up -d
docker compose exec -T django python manage.py check
```

**Ova uputa** dolazi na server automatski s `git pull` — nema zasebnog koraka.

### Redoslijed pri prvom web go-live

1. `git pull` + `./scripts/deploy.sh` (backend + mreže + web imagei)
2. `provision_platform_dns` (jednokratno)
3. `docker compose up -d` (web servisi + Traefik labele)
4. Admin: TenantDomain zapisi + **Provision DNS** po objektu (ili `rollout_uzorita_domains`)
5. Traefik `Host()` labele za custom domene izvan `*.stay.hr` (Uzorita već u compose-u)
6. Provjera curl + `is_verified=True`

---

## F) Hospira — sync-versions za detail ekran

Backend dodaje **per-rezervacija hash** u `GET /api/v1/reception/sync-versions/` (query param `reservation_id`). Flutter detail ekran (`uzorita_flutter`) koristi to pri resume-u umjesto uvijek punog `GET /reservations/{id}/`.

**Redoslijed:** **backend prvo** → zatim novi Hospira build.

| Stavka | Potrebno ručno? |
|--------|-----------------|
| Nova Django migracija | **Ne** |
| Novi env varijable | **Ne** |
| Cloudflare / Traefik / DNS | **Ne** |
| Admin konfiguracija | **Ne** |
| Deploy koda na server | **Da** — [Deploy s GitHuba](#deploy-s-githuba) |

API je **backward compatible**: pozivi bez `reservation_id` vraćaju isti JSON kao prije (bez polja `reservation_detail`).

### Brza potvrda u containeru (nakon deploya)

```bash
docker compose exec -T django python manage.py shell -c \
  "import inspect; from apps.reservations.sync_versions import build_sync_versions_payload; \
   print('reservation_id' in inspect.signature(build_sync_versions_payload).parameters)"
```

Očekivano: `True`.

### Provjera API-ja

Zamijeni `<TOKEN>` device tokenom recepcije (Hospira tablet).

**Bez reservation_id** (postojeće):

```bash
curl -sS -H "Authorization: Bearer <TOKEN>" \
  "https://api.stay.hr/api/v1/reception/sync-versions/?year=2026"
```

Očekivano: `200`, JSON s `reservations`, `rooms`, `statistics` — **bez** `reservation_detail`.

**S reservation_id** (novo):

```bash
curl -sS -D - -H "Authorization: Bearer <TOKEN>" \
  "https://api.stay.hr/api/v1/reception/sync-versions/?year=2026&reservation_id=757"
```

Očekivano: `200`, header `ETag`, JSON s `"reservation_detail": "<16 hex znakova>"`.

**304 Not Modified** — ponovi s `If-None-Match: W/"<ETAG>"` iz prethodnog odgovora.

**404** — nepostojeći ID:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <TOKEN>" \
  "https://api.stay.hr/api/v1/reception/sync-versions/?year=2026&reservation_id=999999999"
```

Promijeni status rezervacije #757, ponovi sync-versions — `reservation_detail` i `ETag` moraju se promijeniti.

### Test suite (opcionalno, na serveru)

```bash
docker compose build django
docker compose run --rm django python manage.py test \
  apps.reservations.tests.test_sync_versions \
  apps.api.tests.test_reception_api.ReceptionAPITests.test_sync_versions \
  apps.api.tests.test_reception_api.ReceptionAPITests.test_sync_versions_with_reservation_id \
  apps.api.tests.test_reception_api.ReceptionAPITests.test_sync_versions_reservation_id_not_found \
  -v 2
```

Detalji lokalnog testiranja (Windows): [Test suite](../development/test-suite.md).

### Nakon backend deploya — Flutter

**Repo:** [github.com/avrcanio/uzorita_flutter](https://github.com/avrcanio/uzorita_flutter) (`hr.finestar.hospira`)

1. Pull/build s `syncVersions(reservationId: …)` + `refreshIfStale` na detail ekranu.
2. Distribucija na tablet (Play Store / sideload / MDM).
3. Stari Flutter app i dalje radi (puni detail GET); nema breaking change.
4. Flutter **prije** backend deploya: nedostaje `reservation_detail` → app radi puni detail fetch (safe fallback).

### Rollback

```bash
git checkout <prethodni-commit>
./scripts/deploy.sh
```

### Checklist — Hospira sync-versions

| # | Korak | OK |
|---|--------|-----|
| 1 | `deploy.sh` → `Done.`, `manage.py check` OK | |
| 2 | Container: `reservation_id` u `build_sync_versions_payload` → `True` | |
| 3 | curl bez `reservation_id` — nema `reservation_detail` | |
| 4 | curl s `reservation_id` — `reservation_detail` + `ETag` | |
| 5 | `304` s `If-None-Match` | |
| 6 | `404` za nepostojeći ID | |
| 7 | Hash se mijenja nakon promjene rezervacije | |
| 8 | Hospira Flutter build na tabletu | |

---

## Checklist — Uzorita

| # | Korak | OK |
|---|--------|-----|
| 1 | `provision_platform_dns` (app + wildcard) | |
| 2 | Traefik labele: `app.stay.hr`, `HostRegexp *.stay.hr` | |
| 3 | TenantDomain `uzorita.stay.hr` (tenant hub, property=null) + Provision DNS | |
| 4 | TenantDomain `uzorita-sibenik.stay.hr` (property=uzorita, **primary**) + Provision DNS | |
| 5 | Traefik eksplicitni `Host(uzorita-sibenik.stay.hr)` + valjani LE cert | |
| 6 | `https://uzorita-sibenik.stay.hr/` radi (booking + site-context) | |
| 7 | Guest check-in linkovi koriste `https://uzorita-sibenik.stay.hr` | |
| 8 | `https://app.stay.hr/` recepcija | |
| 9 | Legacy `booking.uzorita.hr` (opcionalno; samo ako `uzorita.hr` zona postoji) | |
---

## Troubleshooting

| Simptom | Provjeri |
|---------|----------|
| `502 Bad Gateway` | Je li web container up? `docker compose ps` |
| DNS ne resolve | Cloudflare dashboard → DNS zapisi; `Provision DNS` ponovno |
| TLS greška / DEFAULT CERT | Eksplicitni `Host()` + `tls.certresolver=cloudflare`; `CF_DNS_API_TOKEN` u traefik **i** stay.hr `.env` mora biti valjan; DNS proxied |
| Booking prikazuje krivi objekt | `TenantDomain.property` FK; `site-context` s `Host` headerom |
| `404` na custom domeni | Nedostaje Traefik `Host()` label za tu domenu |
| Check-in link na staroj domeni | `is_primary` na željenom `TenantDomain`; `is_verified=True` |
| Admin `is_verified` blokira | U produkciji middleware filtrira neverified domene — postavi verified nakon provjere |
| Web ne vidi tenant | Prosljeđuje li edge `Host` header na `STAY_API_INTERNAL_URL`? |

---

## Što backend ne automatizira (MVP)

- **Traefik router labele** — ručno u `docker-compose.yml` (custom domene izvan wildcarda)
- **TLS certifikati** — Traefik ACME automatski nakon DNS-a
- **Cloudflare WAF / Page Rules** — po potrebi u CF dashboardu

---

## Povezani dokumenti

- [README — operacije i quick start](../../README.md#operations)
- [Test suite](../development/test-suite.md)
- `scripts/cloudflare_dns_upsert.sh` — legacy bootstrap za api/admin hostove
