# Channex demo property

**Channex PMS certification** runs on stay.hr tenant **`demo` (id=1)**, property **`channex-demo`**.

Migrate from uzorita (one-time):

```bash
docker exec stay_django python manage.py migrate_channex_cert_to_demo
```

## stay.hr (tenant Demo, id=1)

```bash
docker compose run --rm django python manage.py seed_channex_demo_property
```

Creates property **Test Property - Stay.hr** (`slug=channex-demo`) with units:

| Unit | Booking test room | Max guests | Adults | Children | Infants |
|------|-------------------|------------|--------|----------|---------|
| `BCOM-HOLIDAY` | Holiday Home (1074503007) | 11 | 11 | 0 | 0 |
| `BCOM-STUDIO` | Studio (1074503008) | 2 | 2 | 0 | 0 |

Kapacitet se drži na **`Unit`** (Booking.com room type), ne na `Property`.

Admin: [tenant 1](https://admin.stay.hr/admin/tenants/tenant/1/change/) → property `channex-demo`

## Channex staging — Create Property form

Use the same values when creating the property in Channex UI (must match channel currency **GBP** for hotel `10745030`):

| Field | Value |
|-------|--------|
| Title | `Test Property - Stay.hr` |
| Default Currency | **GBP** |
| Property Type | Hotel |
| Email | `room_reservations@uzorita.hr` |
| Phone | `+385915000000` |
| Website | `https://www.uzorita.hr` |
| Country | Croatia |
| Timezone | Europe/Zagreb |
| Postal Code | `22000` |
| City | Šibenik |
| Address | Ul. bana Josipa Jelačića 58 |
| Latitude | `43.7357` |
| Longitude | `15.8952` |

## Channex room type UUIDs (Test Property - Stay.hr)

| stay.hr unit | Channex room type UUID |
|--------------|-------------------------|
| `BCOM-HOLIDAY` | `430b1381-dace-44d6-8d5d-a0a1025819fc` |
| `BCOM-STUDIO` | `18c437d7-13e3-4dbc-9565-48fad4832bf5` |

### BCOM-STUDIO rate plans (Channex)

| Title | Channex rate plan UUID | Default | Booking rate ID |
|-------|------------------------|---------|-----------------|
| Standard rate | `aa73125c-b9b6-48a7-862f-da68c6e77999` | 95 GBP | 39950621 |
| non-refundable rate | `6734ae1e-70bb-4217-b668-2aa8720bca13` | 79 GBP | 39950622 |

Channel Mapping: Studio → oba rate plana na gornje UUID-e.

### BCOM-HOLIDAY rate plans (Channex)

| Title | Channex rate plan UUID | Default | Booking rate ID |
|-------|------------------------|---------|-----------------|
| Standard rate | `81061916-cc0a-4b78-850e-2d6d4be7c551` | 165 GBP | 39950621 |
| non-refundable rate | `69f4bf3b-e23a-409e-be16-50a0f2b17605` | 135 GBP | 39950622 |

Map Booking.com channel: Holiday Home → Holiday Home, Studio → Studio.

**Property ID:** `e00e6034-c154-4754-b5d9-9fff73ad12f6` (Test Property - Stay.hr, GBP)

**Channel ID:** `8ee9c7aa-6433-4037-924b-4f95598782d5` (title: Demo tenant, property: Test Property - Stay.hr, hotel `10745030`).

## Webhook (Channex UI)

| Field | Value |
|-------|--------|
| **Callback URL** | `https://api.stay.hr/api/v1/integrations/channex/webhook/` |
| **Trigger** | Booking (any event) |
| **Property** | Test Property - Stay.hr |
| **Is Active** | yes |
| **Send Data** | yes |

**Request Params** (append to callback URL):

```json
{
  "provider": "stay",
  "env": "staging"
}
```

**Headers**:

```json
{
  "X-Stay-Channex-Webhook": "<CHANNEX_WEBHOOK_SECRET from .env>"
}
```

Run `python manage.py seed_channex_booking_test_property` (default tenant `demo`) to print/sync secret if missing.

Legacy uzorita cert data lived on tenant `uzorita` / property `channex-bcom-test`; use `migrate_channex_cert_to_demo` to switch.

## Booking ingest (cert test 11)

On `booking_*` webhooks, stay.hr:

1. `GET /booking_revisions/{revision_id}` (single revision — do **not** use `GET /bookings` or list `GET /booking_revisions`)
2. Upserts `Reservation` on property **`channex-demo`** (from `certification_property_slug`)
3. `POST /booking_revisions/{revision_id}/ack`

Manual reprocess:

```bash
docker exec stay_django python manage.py process_channex_booking_revision <revision_uuid>
```

Fallback for missed webhooks (non-acked revisions):

```bash
docker exec stay_django python manage.py channex_booking_revisions_feed --tenant-slug demo
```

Uses `GET /booking_revisions/feed` only; each revision is then fetched by ID and acknowledged.

### Booking status layers (cancel safety)

| Layer | Mechanism | Role |
|-------|-----------|------|
| 1 | Webhook → booking revision ingest | Real-time |
| 2 | Celery Beat every 15 min → `channex_booking_revisions_feed` | Missed webhooks (non-acked feed) |
| 3 | Daily ~06:45 → `reconcile_channex_cancelled_bookings` | Missed **cancels** that left the feed |

Layer 3 is **cancel-only**: it asks `GET /bookings/:id` whether a local `expected`/`checked_in` Channex reservation is remotely `cancelled`. It does **not** sync dates/amounts or treat remote `new`/`modified` as authoritative. HTTP 404 is fail-closed (`remote_not_found`) — never auto-cancel.

Ops (uzorita production):

```bash
# Dry-run (get_booking only)
docker exec stay_django python manage.py reconcile_channex_cancelled_bookings \
  --tenant-slug uzorita --dry-run

# Apply heals
docker exec stay_django python manage.py reconcile_channex_cancelled_bookings \
  --tenant-slug uzorita
```

## ARI push (cert tests 1–10)

stay.hr stores daily availability + rates in DB, queues changes in `ChannexAriOutbox`, then pushes to Channex.

### Setup

```bash
docker exec stay_django python manage.py seed_channex_rate_plans --tenant-slug demo
```

### Cert test 1 — full sync (500 days, 2 API calls)

```bash
docker exec stay_django python manage.py channex_ari_full_sync --tenant-slug demo
```

Returns Channex **task IDs** in output — use for certification form.

Full sync `POST /restrictions` batches include all declared restriction fields per month range:

- `rate`, `min_stay_arrival`, `min_stay_through`, `max_stay`
- `stop_sell`, `closed_to_arrival`, `closed_to_departure`

### Cert tests 2–8 — change rates/restrictions (PMS API)

`PATCH https://api.stay.hr/api/v1/integrations/channex/ari/rates/`  
Auth: API application with scope `reception:write`

Single date (test 2):

```json
{
  "updates": [
    {
      "unit_code": "BCOM-STUDIO",
      "rate_plan_code": "standard",
      "date": "2026-11-22",
      "rate": "333.00"
    }
  ]
}
```

Multiple rates, one call (test 3) — send all updates in one request body; they merge into one Channex `POST /restrictions`.

Date range (test 4):

```json
{
  "updates": [
    {
      "unit_code": "BCOM-STUDIO",
      "rate_plan_code": "standard",
      "date_from": "2026-11-01",
      "date_to": "2026-11-10",
      "rate": "241.00"
    }
  ]
}
```

**Delta push:** Channex receives only fields you send in the PATCH body. Example: test #2 with only `"rate": "333.00"` produces a restrictions payload with `date`, `property_id`, `rate_plan_id`, and `rate` — not a full snapshot of other restrictions.

Optional restriction fields: `min_stay_arrival`, `min_stay_through`, `max_stay`, `stop_sell`, `closed_to_arrival`, `closed_to_departure`.

Availability: `PATCH .../channex/ari/availability/` with `unit_code`, `date` or range, `availability`.

Admin: edit `Rate plan day` / `Unit availability day` then run `channex_ari_flush --tenant-slug demo` (or API auto-pushes on PATCH).

### Re-cert checklist (after code deploy)

1. `seed_channex_rate_plans --tenant-slug demo` (if needed)
2. `channex_ari_full_sync --tenant-slug demo` — verify restrictions payloads in Channex logs include all fields above
3. Test #2: PATCH rates with **only** `rate` for `BCOM-STUDIO` / `standard` / `2026-11-22`
4. Test #11: trigger booking via Channex webhook; API log should show only `GET .../booking_revisions/{uuid}` and `POST .../ack`
5. Optional: `channex_booking_revisions_feed --tenant-slug demo`
6. Submit new certification form

Google cert obrazac (podaci za popunjavanje): [channex-certification-form.md](./channex-certification-form.md)

Automatski testovi: [test-suite.md](../development/test-suite.md)

### Rate plan codes (test property)

| Unit | Code | Channex rate plan |
|------|------|-------------------|
| BCOM-STUDIO | `standard` | `aa73125c-…` |
| BCOM-STUDIO | `non_refundable` | `6734ae1e-…` |
| BCOM-HOLIDAY | `standard` | `81061916-…` |
| BCOM-HOLIDAY | `non_refundable` | `69f4bf3b-…` |
