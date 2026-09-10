# Channel manager — onboarding po tenantu

Operativni runbook za postavljanje **jednog** outbound connectora po tenantu: `none` ili `channex`.

**Izvor istine za cijene:**

| Cjenik | Model | Push |
|--------|--------|------|
| **Booking.com** | `ChannelRatePlan.sales_channel=booking_com` + `RatePlanDay` | Channex ARI → Booking.com |
| **Direct / stay** | `ChannelRatePlan.sales_channel=direct` + `RatePlanDay` | Nema (lokalno u stay.hr) |
| **Airbnb** (priprema) | `sales_channel=airbnb` | Faza 2 — Channex UUID po kanalu |

Recepcija → Kalendar: prekidač **Naše cijene** / **Booking.com** filtrira API (`?sales_channel=`).

Direct cjenik se pri migraciji kopira iz postojećih B.com cijena; zatim se uređuje neovisno. Komanda za ručno dopunjavanje:

```bash
docker compose exec django python manage.py seed_direct_rate_plans --tenant-slug uzorita
```

**Repo na serveru:** `/opt/stacks/stay.hr`  
**Admin:** https://admin.stay.hr/admin/  
**Recepcija (web):** https://app.stay.hr/

Povezano: [domain-setup.md](domain-setup.md) (domene, deploy), [README — operacije](../../README.md#operations).

---

## Načelo

Svaki tenant ima **točno jedan** `channel_manager` u `TenantReceptionSettings`:

| Mod | Outbound (stay.hr → kanal) | Inbound rezervacije |
|-----|----------------------------|---------------------|
| `none` | — | PDF/XLS import, ručni unos |
| `channex` | Channex ARI (full sync + delta) | Channex webhook |

Referentni tenant-i u produkciji:

| Tenant | `channel_manager` | Namjena |
|--------|-------------------|---------|
| `demo` | `channex` | Channex PMS certifikacija (staging) |
| `uzorita` | `channex` | Produkcija — live Booking.com preko Channexa |
| novi tenant | `none` | Bez connectora dok se ne odluči mod |

UI na recepciji (`app.stay.hr`) prilagođava se preko `feature_flags` iz `GET /api/v1/reception/app-config/`:

| Flag | `none` | `channex` |
|------|--------|-----------|
| `channel_panel` | ✗ | ✓ (Channel stranica) |
| `calendar_blocks` | ✓ | ✓ |
| `reception_create_reservation` | ✓ | ✓ |
| `manual_import` | ✓ | ✓ |

Staff mora biti ulogiran na **ispravan tenant** (`TenantMembership`). Korisnik s pristupom više tenantima bira tenant na login ekranu.

---

## Preduvjeti

1. Tenant postoji s property-jima i unit-ima (`code` mora odgovarati mappingu — npr. `R1`, `BCOM-STUDIO`).
2. U `.env` na serveru:
   - `STAY_INTEGRATION_FERNET_KEY` — obavezno za enkriptirane credential-e u bazi
   - Po modu: `CHANNEX_*` (vidi sekciju Channex ispod)
3. Django deploy aktualan (`./scripts/deploy.sh` nakon pull-a s backend promjenama).
4. Za Channex inbound: webhook URL mora biti dostupan s interneta (`https://api.stay.hr/...`).

---

## Zajednički koraci (svi modovi)

**Tko:** platform superuser (Django admin)

### 1. Tenant

Admin → [Tenants](https://admin.stay.hr/admin/tenants/tenant/) → **Add** (ili odaberi postojeći):

- `name`, `slug`, `status=active`, `timezone`, `default_language`
- Inline **Tenant domains** — vidi [domain-setup.md](domain-setup.md)
- Inline **Reception settings** — `channel_manager` postavlja se u koraku moda (dolje)

CLI (samo demo scaffold):

```bash
cd /opt/stacks/stay.hr
docker compose run --rm django python manage.py seed_demo_tenant
```

### 2. Property + unit-i

Admin → Properties / Units za taj tenant. Svaki unit treba stabilan `code` (npr. `R1`) koji se mapira na Channex room type UUID.

### 3. Staff korisnici

Admin → [Users](https://admin.stay.hr/admin/auth/user/) → **Add user**:

1. Username, lozinka, uključi **Staff status** (ne superuser osim namjerno).
2. U inline **Tenant access** dodaj tenant(e).
3. Korisnik se prijavljuje na https://app.stay.hr/login (ili admin za operativne zapise unutar tenanta).

### 4. Provjera sessiona

1. Login na `app.stay.hr` kao staff.
2. Ako korisnik ima više tenant membership-a, odaberi ispravan tenant.
3. Otvori timeline — rezervacije i property-i moraju pripadati tom tenantu.
4. (Opcionalno) API provjera:

```bash
# Zamijeni <SESSION_COOKIE> nakon browser login-a, ili koristi staff API token ako postoji.
curl -sS -b "sessionid=<SESSION_COOKIE>" \
  "https://api.stay.hr/api/v1/reception/app-config/" | jq '.channel_manager, .feature_flags'
```

---

## Mod A — `none` (Manual)

**Kada:** novi tenant bez channel connectora; ručni/PDF/XLS import.

### Koraci

1. Admin → Tenant → **Reception settings** → `channel_manager` = **Manual** (`none`).
2. Spremi — nije potreban `IntegrationConfig` za outbound.
3. Recepcija: PDF import na timelineu; XLS trenutno samo CLI (`import_booking_xls`).

### Outbound ponašanje

- Nova ručna rezervacija (`import_source=manual`) — **nema** pusha u kanal.
- Otkaz / promjena datuma — lokalno u stay.hr, bez vanjskog synca.

### Checklist

| # | Korak | OK |
|---|--------|-----|
| 1 | `channel_manager=none` spremljen bez validation errora | |
| 2 | Staff login → ispravan tenant | |
| 3 | `feature_flags.reception_create_reservation=true` | |
| 4 | Nova rezervacija kroz **New reservation** radi | |

---

## Mod B — `channex`

**Kada:** Channex PMS certifikacija (`demo`) ili produkcijski/staging Channex tenant.

### Podmodovi

| Tenant | `use_generated_ari` | Full sync izvor |
|--------|----------------------|-----------------|
| `demo` (cert) | `true` | Generirani ARI (cert test hotel) |
| produkcija | `false` | Inventory iz stay.hr (`UnitAvailabilityDay`, rezervacije, blockovi) |

### Koraci — cert tenant (`demo`)

Jednokratni bundle (property + units + IntegrationConfig + rate planovi):

```bash
cd /opt/stacks/stay.hr
docker compose exec django python manage.py migrate_channex_cert_to_demo
```

Ili korak po korak:

```bash
docker compose exec django python manage.py seed_channex_booking_test_property \
  --tenant-slug demo --deactivate-other-tenants
docker compose exec django python manage.py seed_channex_rate_plans --tenant-slug demo
```

Credentiali za produkciju drže se u **IntegrationConfig** (baza), ne u `.env`. Vidi [channex-credentials-migration.md](channex-credentials-migration.md).

Admin → Integration configs → Channex → API key, webhook secret, property ID.

Bootstrap (jednokratno, ako baza prazna):

```bash
export CHANNEX_API_KEY='...'
export CHANNEX_PROPERTY_ID='...'
export CHANNEX_WEBHOOK_SECRET='...'
docker compose exec django python manage.py sync_channex_credentials --tenant-slug <slug>
```

Admin → Tenant `demo` → Reception settings → `channel_manager` = **Channex**.

Full sync (cert test 1 — 500 dana):

```bash
docker compose exec django python manage.py channex_ari_full_sync --tenant-slug demo --days 500
```

Ili recepcija → **Channel** → **Full Sync (500 days)**.

### Koraci — produkcijski / staging tenant (npr. `uzorita` s Channexom)

```bash
# Credentiali: admin Integration configs ili sync_channex_credentials (vidi channex-credentials-migration.md)

docker compose exec django python manage.py seed_uzorita_channex_config \
  --tenant-slug uzorita \
  --property-slug uzorita \
  --environment staging    # ili production
```

```bash
docker compose exec django python manage.py seed_channex_rate_plans --tenant-slug uzorita
docker compose exec django python manage.py sync_channex_credentials --tenant-slug uzorita
```

Admin → `channel_manager` = **Channex**.

U IntegrationConfig admin formi:

- `use_generated_ari` = **false** za produkciju
- `sync_property_slug` = slug property-ja čiji se inventory synca
- `room_types` JSON: `unit_code`, `channex_room_type_id`, `channex_title`, `unit_id`

Full sync nakon postavljanja:

```bash
docker compose exec django python manage.py channex_ari_full_sync --tenant-slug <slug> --days 500
```

### Channex webhook (inbound booking)

**URL:**

```text
https://api.stay.hr/api/v1/integrations/channex/webhook/?provider=stay&env=staging
```

(Za production Channex account promijeni `env` u query parametru ako konfiguracija to zahtijeva — default u kodu je `staging`.)

**Channex UI — webhook postavke:**

| Polje | Vrijednost |
|-------|------------|
| Callback URL | URL gore |
| Custom header | `X-Stay-Channex-Webhook: <CHANNEX_WEBHOOK_SECRET>` |
| Query params | `provider=stay`, `env=staging` |

Secret mora odgovarati `webhook_secret` u **IntegrationConfig** za taj tenant/property (ne globalni `.env`).

Health check (401 bez headera je očekivano):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  "https://api.stay.hr/api/v1/integrations/channex/webhook/?provider=stay&env=staging"
```

Test booking iz Channex cert-a → rezervacija u stay.hr s `import_source=channex` (bez outbound loopa).

### Mapiranje unit-a → room type UUID

1. U Channex kreiraj room type po fizičkoj jedinici (`count=1`).
2. Kopiraj UUID u `room_types` / `booking_test_rooms` JSON.
3. `unit_code` mora odgovarati stay.hr `Unit.code`.
4. Pokreni seed ponovno ili ručno uredi JSON u adminu.

Cert demo sobe definirane su u `apps/integrations/channex/booking_test.py` (`BCOM-STUDIO`, itd.).

### Outbound ponašanje

- Full sync + delta rate/availability — Channel panel na recepciji.
- Nova ručna rezervacija → Channex availability delta (−1 po noći).
- Otkaz / promjena datuma → delta prema novom stanju.
- Inbound Channex booking → **ne** okida outbound (loop prevention).

### Checklist

| # | Korak | OK |
|---|--------|-----|
| 1 | Aktivan Channex `IntegrationConfig`, credentials OK u adminu | |
| 2 | `channel_manager=channex` spremljen | |
| 3 | Room type mapping s `unit_id` za sve sobe | |
| 4 | `seed_channex_rate_plans` pokrenut | |
| 5 | Full sync — `task_ids` u push results / CLI output | |
| 6 | Webhook registriran, secret match | |
| 7 | Test inbound booking vidljiv na timelineu | |
| 8 | Channel panel vidljiv na recepciji | |

---

## Admin reference

| Što | Gdje |
|-----|------|
| `channel_manager` | Admin → Tenants → tenant → **Reception settings** inline |
| Credential-i + mapping | Admin → Integrations → **Integration configs** |
| Rate planovi (Channex) | Admin → Integrations → **Channel rate plans** (seed iz configa) |
| Outbox / failed push | Admin → Integrations → **Channex ARI outbox** |
| Staff pristup tenantu | Admin → Users → **Tenant access** inline |

Validacija: spremanje `channel_manager=channex` bez aktivnog Channex `IntegrationConfig` vraća grešku na formi.

---

## Management commandi (sažetak)

| Command | Namjena |
|---------|---------|
| `seed_demo_tenant` | Scaffold demo tenant + domain + units |
| `seed_channex_booking_test_property` | Cert property + units + Channex config |
| `migrate_channex_cert_to_demo` | Cert bundle na tenant `demo` |
| `seed_uzorita_channex_config` | Channex config (staging room types) |
| `seed_channex_rate_plans` | `ChannelRatePlan` redovi iz configa (`sales_channel=booking_com`) |
| `seed_direct_rate_plans` | Kopira B.com planove u `sales_channel=direct` (+ `RatePlanDay`) |
| `sync_channex_credentials` | Merge API key / webhook / property ID iz env |
| `channex_ari_full_sync` | Push 500-day ARI u Channex |
| `channex_ari_flush` | Retry pending outbox (operativno) |
| `channex_ari_abandon_failed` | Terminalno zatvaranje starih `FAILED` outbox redova (`ABANDONED`) |

Svi commandi:

```bash
cd /opt/stacks/stay.hr
docker compose exec django python manage.py <command> --help
```

---

## Disposition za `FAILED` ARI outbox redove

`flush_channex_ari_outbox` bira **samo** `PENDING` redove i na grešci označi red `FAILED` pa re-raisea. `FAILED` red se dakle ne flusha sam, a ponovni `channex_ari_flush` ga neće pokupiti.

**Stari `FAILED` ARI snapshot ne smije se izravno requeueati.** Payload je point-in-time stanje i može pokrivati datume daleko u budućnost, pa bi ponovno slanje pregazilo dostupnost koja je u međuvremenu postala ispravna. Prvo se trenutno stanje ponovno uskladi i verificira canonical putem (`channex_ari_full_sync`, `FULL_SYNC_DAYS=500`, pa `verify_channex_availability`), nakon čega se stari zapis može terminalno označiti `ABANDONED`.

Za **svjež** failure ovo pravilo ne vrijedi — tamo su istraga ili requeue sasvim legitimni.

`ABANDONED` je terminalno stanje: flush ga po definiciji ne vidi (bira `PENDING`), pa red više ne može biti poslan.

```bash
# 1. dry-run — bez cutoffa prikazuje sve FAILED redove, po tenantu
docker compose exec django python manage.py channex_ari_abandon_failed

# 2. apply — cutoff je obavezan i ekskluzivan (created_at < 00:00 tog dana)
docker compose exec django python manage.py channex_ari_abandon_failed \
  --created-before YYYY-MM-DD --apply
```

Dry-run po tenantu ispisuje broj redova, razbijanje po `kind`, span `created_at`, **span payload datuma** (pokriva oba oblika value zapisa — per-day `date` i komprimirani `date_from`/`date_to`) te je li Channex integracija aktivna. Uz zadan cutoff dodaje `in scope` / `outside cutoff`.

Guardovi:

- `--apply` bez `--created-before` je odbijen — cohort mora biti eksplicitan da svjež `FAILED` red ne bi slučajno završio kao `ABANDONED`
- prije `--apply` dry-run mora potvrditi **točno očekivani cohort**, ne samo ukupan broj: očekivani broj redova **po tenantu** i raspon `created_at`. Novi tenant, novi `kind` ili red izvan očekivanog perioda znače **STOP i pregled**
- komanda ne radi ni jedan Channex poziv i ne traži aktivnu integraciju, pa može zatvoriti i redove dekomisioniranog tenanta

---

## Troubleshooting

| Simptom | Provjeri |
|---------|----------|
| Validation error pri spremanju `channel_manager` | Postoji li aktivan `IntegrationConfig` za taj provider? |
| Recepcija nema Channel panel | `channel_manager` mora biti `channex`; osvježi nakon admin promjene |
| Calendar block nedostaje | Provjeri `feature_flags.calendar_blocks`; Channex tenant koristi lokalni block + availability delta |
| Full sync bez `task_ids` | `CHANNEX_API_KEY`, `property_id`, network; Django log |
| Webhook 401 | `X-Stay-Channex-Webhook` header, query `provider=stay`, secret match |
| Inbound booking ne stiže | Channex webhook log; `property_id` u payloadu mapira na IntegrationConfig |
| Outbound loop (dupli push) | Rezervacije s `import_source=channex` ne bi trebale okidati outbound |
| Channex API / credentials error | Admin → Integration configs; `sync_channex_credentials` |
| Pogrešan tenant u recepciji | `TenantMembership`; logout + login s odabirom tenanta |
| Mapping `unit_id` missing | Unit s tim `code` ne postoji ili nije aktivan prije seeda |

---

## Povezani dokumenti

- [channex-credentials-migration.md](channex-credentials-migration.md) — platforma vs tenant credentiali, produkcija
- [domain-setup.md](domain-setup.md) — domene, deploy, recepcija login
- [README — Django admin](../../README.md#django-admin) — staff vs superuser
- Operativni plan po fazama (full sync, delta, rezervacije) — internal plan `operativne_faze_po_operaciji`
