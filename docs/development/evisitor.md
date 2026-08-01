# eVisitor integracija

stay.hr je **integracijski sloj** između recepcije, OCR/WhatsApp kanala i službenog eVisitor REST API-ja. Ne zamjenjuje eVisitor web sučelje — automatizira prijavu i odjavu gostiju kad su podaci u sustavu potpuni i konfiguracija ispravna.

---

## Arhitektura

```text
  OCR / Hospira          WhatsApp operator          Ručni unos (recepcija)
         \                      |                          /
          \                     |                         /
           v                    v                        v
                    Guest + Reservation
                              |
                              v
              submit_evisitor_for_reservation  /  submit_guest_checkin
                              |
                              v
                    build_check_in_payload (mapper)
                              |
                              v
                       EvisitorClient
                              |
              +---------------+---------------+
              v                               v
      CheckInTourist / CheckOutTourist   EvisitorSubmission (audit)
              |
              v
         eVisitor REST API
```

**Ključni moduli** (`backend/apps/integrations/evisitor/`):

| Modul | Uloga |
|-------|-------|
| `resolver.py` | Učitava `IntegrationConfig` (provider `EVISITOR`) za tenant/objekt |
| `eligibility.py` | Određuje je li gost obavezan za eVisitor (18+) |
| `mapper.py` | Gradi `CheckInTourist` / `CheckOutTourist` payload iz `Guest` |
| `client.py` | Login, REST pozivi, validacija test/prod URL-a |
| `service.py` | Orkestracija: validacija → audit → API → ažuriranje `Guest` |
| `messages.py` | Parsiranje grešaka, recovery UUID iz „već prijavljena” |
| `summary.py` | Agregat `evisitor_summary` na razini rezervacije |

Ulazni podaci gosta dolaze iz OCR pipelinea, WhatsApp operatora ili ručnog unosa u recepciji — vidi [id-document-import.md](id-document-import.md) i [ocr-multi-guest-rules.md](../operations/ocr-multi-guest-rules.md).

---

## Životni ciklus

```text
Konfiguracija
      ↓
Validacija
      ↓
Audit
      ↓
CheckInTourist
      ↓
Boravak
      ↓
CheckOutTourist
```

Audit zapis (`EvisitorSubmission`) postoji i tijekom check-ina i checkouta — kreira se **prije** API poziva i ažurira se nakon odgovora (uspjeh ili greška).

---

## Konfiguracija

Credentials i parametri objekta drže se u **`IntegrationConfig`** s providerom `EVISITOR` ([`resolver.py`](../../backend/apps/integrations/evisitor/resolver.py)).

### Hijerarhija

1. **Property-specific** `IntegrationConfig` (s `property_id`) ima prednost nad tenant defaultom (`property_id = NULL`).
2. Sortiranje: `property_id DESC NULLS LAST` — prvi redak je aktivna konfiguracija.
3. Vremena dolaska/odlaska za payload: `Property.check_in_time` / `check_out_time` (prebivaju default iz config dicta).

### Test vs produkcija

[`client.py`](../../backend/apps/integrations/evisitor/client.py) provjerava usklađenost `env` i `base_url`:

| `env` | `base_url` mora sadržavati | Dodatno |
|-------|---------------------------|---------|
| `test` | `testApi` (ne smije biti samo produkcijski `eVisitorRhetos_API`) | `api_key` je **obavezan** |
| `prod` | `eVisitorRhetos_API` | ne smije biti `testApi` |

Ako integracija nije uključena ili credentials nedostaju, API vraća `config_error` (400).

### Legacy env varijable

Pri migraciji iz starog sustava, `UZORITA_EVISITOR_*` u `.env` mogu se pretvoriti u `IntegrationConfig` putem `migrate_uzorita_legacy` — vidi [`.env.example`](../../.env.example). Produkcijski runtime koristi isključivo `IntegrationConfig` u bazi, ne direktno env.

---

## Demo seed (`seed_evisitor_config`)

Management command [`seed_evisitor_config`](../../backend/apps/integrations/management/commands/seed_evisitor_config.py) upisuje test credentials u `IntegrationConfig` iz `DEMO_EVISITOR_*` env varijabli. Ne provjerava credentials niti zove HTZ API — za to koristite `smoke_evisitor`.

### Env varijable

| Varijabla | Obavezna | Default | Opis |
|-----------|----------|---------|------|
| `DEMO_EVISITOR_USERNAME` | da | — | HTZ korisničko ime |
| `DEMO_EVISITOR_PASSWORD` | da | — | HTZ lozinka |
| `DEMO_EVISITOR_FACILITY_CODE` | da | — | Šifra objekta |
| `DEMO_EVISITOR_BASE_URL` | da | — | npr. `https://www.evisitor.hr/testApi` |
| `DEMO_EVISITOR_API_KEY` | kad je `ENV=test` | — | Test API ključ |
| `DEMO_EVISITOR_ENABLED` | ne | `true` | `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` |
| `DEMO_EVISITOR_ENV` | ne | `test` | `test` ili `prod` |
| `DEMO_EVISITOR_DEFAULT_ARRIVAL_ORGANISATION` | ne | `I` | Default za payload |
| `DEMO_EVISITOR_DEFAULT_OFFERED_SERVICE_TYPE` | ne | `noćenje` | Default za payload |
| `DEMO_EVISITOR_DEFAULT_PAYMENT_CATEGORY` | ne | `14` | Default za payload |
| `DEMO_EVISITOR_DEFAULT_STAY_TIME_FROM` | ne | `15:00` | Default za payload |
| `DEMO_EVISITOR_DEFAULT_STAY_TIME_UNTIL` | ne | `11:00` | Default za payload |

Primjer `.env` bloka:

```bash
DEMO_EVISITOR_USERNAME=...
DEMO_EVISITOR_PASSWORD=...
DEMO_EVISITOR_API_KEY=...
DEMO_EVISITOR_FACILITY_CODE=...
DEMO_EVISITOR_BASE_URL=https://www.evisitor.hr/testApi
DEMO_EVISITOR_ENV=test
DEMO_EVISITOR_ENABLED=true
```

Pokretanje (property-level, default scope `demo/demo`):

```bash
docker compose run --rm django python manage.py seed_evisitor_config
docker compose run --rm django python manage.py seed_evisitor_config --tenant-level
```

### Post-deploy provjera (CI / deploy)

[`scripts/verify-demo-evisitor.sh`](../../scripts/verify-demo-evisitor.sh) se automatski pokreće na kraju [`scripts/deploy.sh`](../../scripts/deploy.sh) kad je u `.env` postavljen `DEMO_EVISITOR_USERNAME`. Preskače se ako demo eVisitor nije konfiguriran.

Redoslijed:

1. `seed_evisitor_config` — idempotentni upis configa
2. `smoke_evisitor --tenant-slug demo --property-slug demo --login-only --json`
3. Validacija JSON izlaza: `status: ok`, `exit_code: 0`, `steps.config: true`, `steps.login: true`

Ako smoke padne, skripta ispisuje cijeli JSON payload (bez secreta) radi dijagnostike.

Ručno:

```bash
./scripts/verify-demo-evisitor.sh
```

---

## Dijagnostika (`smoke_evisitor`)

Management command [`smoke_evisitor`](../../backend/apps/integrations/management/commands/smoke_evisitor.py) je read-only/dijagnostički alat za provjeru eVisitor integracije na demo ili produkciji **bez dupliciranja poslovne logike**. Koristi isti `get_evisitor_config_row`, `resolve_evisitor_config`, `EvisitorClient` i `build_check_in_payload` / `submit_guest_checkin` kao produkcijski kod.

Za postavljanje demo configa u bazu koristite [`seed_evisitor_config`](#demo-seed-seed_evisitor_config); za provjeru konektivnosti `--login-only`, za resolver `--list-config`.

### Primjeri

```bash
# Samo config resolution (bez API poziva)
docker compose run --rm django python manage.py smoke_evisitor \
  --tenant-slug demo --property-slug demo --list-config

# Login/logout test
docker compose run --rm django python manage.py smoke_evisitor \
  --tenant-slug demo --property-slug demo --login-only

# Validacija payloada bez slanja u eVisitor
docker compose run --rm django python manage.py smoke_evisitor \
  --tenant-slug demo --property-slug demo --guest-id 42 --dry-run

# Puni submit (mijenja Guest.evisitor_status i zove HTZ API — test gosti!)
docker compose run --rm django python manage.py smoke_evisitor \
  --tenant-slug demo --property-slug demo --guest-id 42
```

JSON izlaz za CI/automatizaciju (`stdout` samo JSON, bez lozinki i PII):

```bash
docker compose run --rm django python manage.py smoke_evisitor \
  --tenant-slug demo --property-slug demo --list-config --json
```

### Exit codeovi

| Code | Uzrok |
|------|-------|
| `0` | PASS (uključujući `already_sent`, `recovered`) |
| `1` | Nedostaje config, `EvisitorConfigError`, nevaljani flagovi |
| `2` | Login nije uspio |
| `3` | Gost nije obavezan (`not_required`) ili `EvisitorValidationError` (mapper) |
| `4` | `EvisitorApiError` pri submitu |

### Demo guest seed (`seed_demo_guest`)

Management command [`seed_demo_guest`](../../backend/apps/tenants/management/commands/seed_demo_guest.py) stvara jednu idempotentnu rezervaciju (`MOCK-EVISITOR-001`) s primarnim gostom koji ima sva polja potrebna za `build_check_in_payload`. Ne mijenja `evisitor_status` postojećeg gosta i ne zove HTZ API.

```bash
docker compose run --rm django python manage.py seed_demo_guest
docker compose run --rm django python manage.py seed_demo_guest --json
```

JSON izlaz: `{ "guest_id": N, "reservation_id": M, "booking_code": "MOCK-EVISITOR-001" }`.

### Bootstrap demo eVisitor

Skripta [`scripts/bootstrap-demo-evisitor.sh`](../../scripts/bootstrap-demo-evisitor.sh) povezuje postojeće alate u jedan QA/onboarding korak:

```bash
./scripts/bootstrap-demo-evisitor.sh              # login + dry-run (default)
./scripts/bootstrap-demo-evisitor.sh --login-only # tenant + config + guest + login
./scripts/bootstrap-demo-evisitor.sh --submit     # uključuje stvarni HTZ submit (opt-in)
```

Redoslijed: `seed_demo_tenant` → `seed_evisitor_config` (preskače se ako `DEMO_EVISITOR_USERNAME` nije set) → `seed_demo_guest` → `smoke_evisitor --list-config` → `--login-only` → `--guest-id --dry-run` (osim `--login-only`).

### Demo workflow

```text
seed_demo_tenant
    ↓
seed_evisitor_config
    ↓
seed_demo_guest
    ↓
smoke_evisitor --list-config
    ↓
smoke_evisitor --login-only
    ↓
smoke_evisitor --guest-id N --dry-run
    ↓
smoke_evisitor --guest-id N
```

1. `seed_demo_tenant` — demo tenant i property (`demo`/`demo`).
2. `seed_evisitor_config` — upis `IntegrationConfig` iz `DEMO_EVISITOR_*` env-a.
3. `seed_demo_guest` — rezervacija `MOCK-EVISITOR-001` + eVisitor-spreman gost (`guest_id` u izlazu).
4. `--list-config` — provjera scope-a i facility koda (bez API poziva).
5. `--login-only` — provjera credentials i URL-a.
6. `--dry-run --guest-id` — lokalna validacija payloada.
7. `--guest-id` — puni submit samo na test gostu s poznatim podacima.

---

## Eligibility

Funkcija `guest_requires_evisitor` ([`eligibility.py`](../../backend/apps/integrations/evisitor/eligibility.py)):

- Gost mora imati **18+ godina na dan dolaska** (`reservation.check_in`).
- Ako je `date_of_birth` **null**, gost se tretira kao **obavezan** za eVisitor (sigurnosna pretpostavka dok datum nije poznat).
- Djeca mlađa od 18 godina: operacijski status `not_required` — ne ulaze u submit workflow niti u `evisitor_summary` agregat.

---

## Check-in u eVisitor

**Check-in rezervacije ≠ eVisitor submit.** Promjena `Reservation.status` na `checked_in` ne garantira automatsku prijavu u eVisitoru; submit je zaseban korak (ručno ili automatizirano nakon check-ina).

### Koraci submita

1. **Lokalna validacija** — `build_check_in_payload` provjerava obavezna polja (`first_name`, `last_name`, `sex`, `date_of_birth`, državljanstvo, dokument, adresa…). Greška → `validation_failed` **bez** kreiranja `EvisitorSubmission`.
2. **Audit** — kreira se `EvisitorSubmission` sa statusom `pending`; `Guest.evisitor_status` → `pending`.
3. **Login** — `EvisitorClient.login()` (AspNetFormsAuth + opcionalni `apikey` na testu).
4. **CheckInTourist** — POST na `/Rest/Htz/CheckInTourist/`.
5. **Uspjeh** — submission → `sent`, `Guest.evisitor_status` → `sent`, spremljen `evisitor_registration_id`.

### Okidači submita

| Okidač | Put | Napomena |
|--------|-----|----------|
| Recepcija (Hospira) | `POST .../evisitor-submit/` | Ručni submit po gostu |
| WhatsApp operator — apply job | `complete_guest_checkin_after_apply` | Nakon check-ina + apply dokumenata |
| WhatsApp operator — job complete | `operator_job_complete` | Batch nakon operatorovog završetka |
| Toni — potvrda dolaska | `perform_arrival_confirmed_checkin` | Check-in + eVisitor za sve eligible goste |
| Ops / management commands | npr. `ops_res22_bottcher` | Ručni operativni alati |

OCR sam po sebi **ne šalje** eVisitor — nakon importa dokumenata submit je zaseban korak ([`document_intake_service.py`](../../backend/apps/reservations/document_intake_service.py)).

---

## Recovery („već prijavljena”)

Kad eVisitor vrati grešku da je osoba već prijavljena, backend **ne radi** zasebni GET/fetch prema eVisitoru. Umjesto toga:

1. `parse_existing_registration_id` ([`messages.py`](../../backend/apps/integrations/evisitor/messages.py)) izvlači UUID iz `UserMessage` (templating s `|||` ili regex).
2. Lokalno stanje se usklađuje: `Guest.evisitor_status` → `sent`, `evisitor_registration_id` → izvučeni UUID.
3. `EvisitorSubmission` se označava kao `sent` s `response_payload.recovered: true`.
4. API odgovor uključuje `recovered: true` i čitljivu `message`.

Time recovery ostaje idempotentan i ne ovisi o dodatnim API mogućnostima eVisitora.

---

## Statusi

Postoje **dva sloja** statusa — ne miješati ih u UI-u.

### Trajno stanje modela (`Guest.evisitor_status`)

| status | značenje |
|--------|----------|
| `not_sent` | default — još nije pokušano ili nije uspjelo bez trajnog zapisa |
| `pending` | **Privremeno stanje tijekom obrade zahtjeva prema eVisitor API-ju** (ne bi trebalo dugo ostati) |
| `sent` | uspješno prijavljeno u eVisitor |
| `checked_out` | odjavljeno iz eVisitora |
| `failed` | **CheckIn nije uspio**; gost možda nije u eVisitoru — moguće ponoviti prijavu |
| `checkout_failed` | **CheckIn uspješan, CheckOut nije uspio**; gost je još prijavljen — ponoviti odjavu (ne prijavu) |

**Invariant:**

```
failed
  = CheckIn nije uspio

checkout_failed
  = CheckIn uspješan
  = CheckOut nije uspio
  = gost je još prijavljen u eVisitoru
  = evisitor_registration_id i uspješni check-in submission ostaju netaknuti
```

Zabranjena tranzicija: `checkout_failed` → `failed`.

### Operacijski status (rezultat jedne operacije / API odgovor)

Ovo **nije** `Guest.evisitor_status` — vraća se u JSON odgovoru endpointa ili u listi rezultata `submit_evisitor_for_reservation`:

| status | značenje |
|--------|----------|
| `not_required` | dijete &lt;18 |
| `validation_failed` | lokalna validacija prije API poziva |
| `config_error` | nedostaje/neispravna konfiguracija |
| `failed` / `api_error` | eVisitor ili mrežna greška |

### Agregat rezervacije (`evisitor_summary`)

Funkcija `evisitor_summary_for_reservation` ([`summary.py`](../../backend/apps/integrations/evisitor/summary.py)):

| vrijednost | značenje |
|------------|----------|
| `none` | nema gostiju |
| `incomplete` | barem jedan eligible gost nije prijavljen (`sent` / `checkout_failed` / `checked_out`) |
| `complete` | svi eligible gosti su **prijavljeni** (`sent`, `checkout_failed` ili mix s `checked_out`) — **ne** znači da je rezervacija odjavljena |
| `checked_out` | svi eligible gosti su `checked_out` |

Ako nema eligible gostiju (samo djeca), summary je `complete`.

> `summary == complete` dopušta pokušaj reservation checkouta. Ako CheckOut padne, rezervacija ostaje `checked_in` uz `CheckoutBlockedError(evisitor_checkout_failed)` dok svi gosti nisu `checked_out`.

---

## Audit (`EvisitorSubmission`)

> Svaki API pokušaj (uspješan ili neuspješan) nakon uspješne lokalne validacije zapisuje se u `EvisitorSubmission`, uključujući maskirani request, response i eventualne greške.

Polja ([`models.py`](../../backend/apps/reservations/models.py)):

| polje | sadržaj |
|-------|---------|
| `request_payload` | maskirani JSON poslan eVisitoru |
| `response_payload` | odgovor ili `{ "error": ..., "system": ... }`; recovery uključuje `recovered: true` |
| `error_user_message` | čitljiva poruka za UI |
| `error_system_message` | tehnički detalj s API-ja |
| `registration_id` | UUID prijave (generiran lokalno ili recovery) |
| `status` | `pending` → `sent` / `failed` / `checked_out` |

**Bitno za debugging:** `validation_failed` nastaje u `build_check_in_payload` **prije** kreiranja `EvisitorSubmission` — takav pokušaj **nema** audit redak.

Checkout koristi isti model: submission se kreira prije `CheckOutTourist`, ažurira se nakon odgovora.

---

## Idempotentnost

1. **`sent` bez `force_retry`** — endpoint ne zove eVisitor; vraća postojeće stanje ([`EvisitorSubmitView`](../../backend/apps/api/reception_views.py), L909–922).
2. **Recovery** — usklađivanje iz greške „već prijavljena” bez duplicirane prijave u eVisitoru.
3. **Checkout** — koristi spremljeni `Guest.evisitor_registration_id` za `CheckOutTourist`; ponovljeni checkout za `checked_out` gosta vraća postojeći submission bez novog API poziva (per-guest endpoint i `submit_guest_checkout`).
4. **Ponovljeni submit** — dok je gost već `sent` ili `checkout_failed`, ponovni check-in pozivi ne stvaraju duplicirane prijave; `checkout_failed` **nikad** ne pokreće CheckIn retry.
5. **Checkout retry** — `checkout_failed` → retry → `checkout_failed` ili `checked_out` (nikad `failed`).

---

## Checkout

stay.hr sprječava zatvaranje rezervacije prije ispunjenja zakonskih obveza prijave gostiju ([`checkout.py`](../../backend/apps/reservations/checkout.py)).

```text
perform_reservation_checkout
         |
         v
  evisitor_summary == complete?   (= svi eligible prijavljeni)
         |
    NE --+--> CheckoutBlockedError (evisitor_incomplete)
         |
        DA
         |
         v
  checkout_reservation_guests_in_evisitor
  (CheckOutTourist za sent | checkout_failed; continue na grešci)
         |
    fail-+--> CheckoutBlockedError (evisitor_checkout_failed + failed_guests[])
         |    guest.status = checkout_failed; rezervacija ostaje checked_in
         |
        OK (svi checked_out)
         |
         v
  Reservation.status -> checked_out
  (+ fiskalizacija/račun ako je konfigurirano)
```

Per-guest retry:

```text
POST /api/v1/reception/reservations/{id}/guests/{guestId}/evisitor-checkout/
```

| status | rezultat |
|--------|----------|
| `sent` / `checkout_failed` | CheckOut |
| `checked_out` | 200 no-op |
| `failed` / `not_sent` / `pending` | 409 |

Observability:

- structured log event `evisitor.checkout_failed` (reservation/guest/registration/correlation/payload/response/reason)
- process-local counter `evisitor_checkout_failed_total` (tenant, property, reason)

Future (van ovog PR-a): `checkout_pending_confirmation` ako eVisitor vrati ambiguous odgovor.

Ako eVisitor konfiguracija nedostaje pri checkoutu, gosti se lokalno označavaju `checked_out` bez API poziva (fallback u `checkout_reservation_guests_in_evisitor`).

---

## WhatsApp obavijest

Nakon uspješnog submita (ili kad je gost već `sent`), backend može poslati gostu WhatsApp poruku da je prijava u eVisitoru gotova:

- Funkcija: `maybe_send_evisitor_registered_whatsapp_reply` ([`evisitor_reply.py`](../../backend/apps/integrations/whatsapp/evisitor_reply.py))
- Env: `WHATSAPP_EVISITOR_REGISTERED_REPLY` (default `true`; `false`/`0`/`off` isključuje)
- Šalje se samo kad je `evisitor_summary` rezervacije `complete` i poruka još nije poslana

Odgovor API-ja može uključivati polje `whatsapp` s rezultatom slanja.

---

## API

### Endpoint

```http
POST /api/v1/reception/reservations/{reservationId}/guests/{guestId}/evisitor-submit/
Authorization: Bearer {api_token}
Content-Type: application/json

{}
```

Opcionalno ponovni pokušaj nakon `failed`:

```json
{ "force_retry": true }
```

Detalji za Flutter/Hospira klijent: [evisitor-flutter.md](evisitor-flutter.md).

### Primjeri odgovora

**Uspjeh (200):**

```json
{
  "status": "sent",
  "registration_id": "a01c2e9f-3839-4f0e-b39b-775e107d6f36",
  "submitted_at": "2026-05-20T15:52:00.803833Z"
}
```

**Recovery — već prijavljena (200):**

```json
{
  "status": "sent",
  "registration_id": "a01c2e9f-3839-4f0e-b39b-775e107d6f36",
  "submitted_at": "...",
  "recovered": true,
  "message": "Osoba Lauriane Saulnier je već prijavljena na datum 20.5.2026. ..."
}
```

**Već `sent` u bazi — bez poziva eVisitora (200):**

```json
{
  "status": "sent",
  "registration_id": "...",
  "message": "Gost je već prijavljen u eVisitor."
}
```

**Validacija (400):**

```json
{
  "status": "validation_failed",
  "message": "Podaci nisu potpuni za eVisitor prijavu.\n...",
  "field_errors": { "date_of_expiry": "..." }
}
```

**Konfiguracija (400):**

```json
{ "status": "config_error", "message": "..." }
```

**eVisitor greška (502):**

```json
{
  "status": "failed",
  "user_message": "...",
  "system_message": "..."
}
```

Za idempotentno ponašanje i recovery vidi sekciju [Idempotentnost](#idempotentnost).

---

## Sažetak

eVisitor integracija u stay.hr slijedi jasan lanac: konfiguracija → lokalna validacija → audit → API. Submit je idempotentan za već prijavljene goste; recovery usklađuje lokalno stanje iz eVisitor greške bez dupliciranih prijava. Checkout blokira zatvaranje rezervacije dok svi eligible gosti nisu `sent`, zatim automatski odjavljuje goste u eVisitoru.

---

## Povezana dokumentacija

- [evisitor-flutter.md](evisitor-flutter.md) — Flutter/Hospira klijent, UI statusi, helper primjeri
- [id-document-import.md](id-document-import.md) — struktura dokumenata, medij, modeli (prvi korak u lancu)
- [ocr-multi-guest-rules.md](../operations/ocr-multi-guest-rules.md) — OCR pipeline, batch, više gostiju
- [ai-runbook-ocr-checkin-evisitor-2026-06.md](../operations/ai-runbook-ocr-checkin-evisitor-2026-06.md) — operativni runbook
- [evisitor-adresa-res-190-2026-05-30.md](../operations/evisitor-adresa-res-190-2026-05-30.md) — format adrese za eVisitor

---

## Izvan opsega

Ovaj dokument **ne** opisuje:

- **Format OCR dokumenata i parsiranje putovnica** — vidi [id-document-import.md](id-document-import.md), [ocr-multi-guest-rules.md](../operations/ocr-multi-guest-rules.md)
- **WhatsApp workflow (operator, Toni)** — vidi [ai-runbook-ocr-checkin-evisitor-2026-06.md](../operations/ai-runbook-ocr-checkin-evisitor-2026-06.md) i [whatsapp-operator-toni-runbook.md](../operations/whatsapp-operator-toni-runbook.md)
- **Fiskalizacija i izdavanje računa pri checkoutu** — vidi billing modul / checkout docs
