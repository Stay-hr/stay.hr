# Channex — Uzorita room type mapping

Four physical units on Channex historically; production now has **five** (each room type has **Count Of Rooms = 1**), including R4.

**Booking.com kanal:** vidi [channex-uzorita-booking-channel.md](channex-uzorita-booking-channel.md) (provider Active 2026-05-26, hotel ID `4181954`).

Staging UUID-ovi u tablici ispod su iz certifikacije; produkcija koristi UUID-e iz [app.channex.io](https://app.channex.io/) property `bca8473d-7c36-4986-bcdb-b5760b633283`.

Booking.com occupancy (max guests, adults, children, infants) mapira se na stay.hr **`Unit`**, ne na `Property`. Polja: `capacity_max_guests`, `capacity_adults`, `capacity_children`, `capacity_infants`.

| stay.hr `Unit.code` | Max | Adults | Children | Infants | Channex / Booking |
|---------------------|-----|--------|----------|---------|-------------------|
| R1 | 2 | 2 | 1 | 1 | Luxury Room Uzorita - R1 |
| R2 | 3 | 2 | 2 | 1 | Luxury Room Uzorita - R2 |
| R3 | 4 | 3 | 3 | 3 | Luxury Room Uzorita - R3 |
| R4 | 2 | 2 | 1 | 1 | Standard King Room R4 |
| R6 | 4 | 3 | 3 | 3 | Deluxe Double Room → R6 |

**Booking.com → Channex channel mapping:** vidi [channex-uzorita-booking-channel.md](channex-uzorita-booking-channel.md) (room ID `418195401`→R1, `418195404`→R2, `418195403`→R3, `418195405`→R6, `418195406`→R4).

Seed occupancy (idempotent):

```bash
docker compose exec django python manage.py seed_uzorita_unit_occupancy
```

Booking.com **Standard Arrangement** (tip kreveta + broj) mapira se na **`UnitBed`** (FK na `Unit`). Vrste: Twin, Full, Queen, King, Bunk, Sofa bed, Futon.

| stay.hr `Unit.code` | Booking napomena | Standard beds (seed) |
|---------------------|-------------------------|----------------------|
| R1 | — | Queen x1, Sofa bed x1 |
| R2 | — | Queen x1, Sofa bed x1 |
| R3 | — | Queen x1, Sofa bed x1 |
| R4 | Booking listing `418195406` | King x1 |
| R6 | Booking listing R6 | Queen x1, Sofa bed x1 |

Seed kreveta:

```bash
docker compose exec django python manage.py seed_uzorita_unit_beds
```

Idempotentno — zamijeni postojeće krevete na navedenim sobama. Override: `--unit-codes R1 R2`.

Booking.com **bathrooms** (broj, private, inside room) mapira se na **`UnitBathroom`** (FK na `Unit`).

| stay.hr `Unit.code` | Standard bathroom (seed) |
|---------------------|--------------------------|
| R1 | 1× private, en-suite |
| R2 | 1× private, en-suite |
| R3 | 1× private, en-suite |
| R4 | 1× private, en-suite |
| R6 | 1× private, en-suite |

Seed kupaonica:

```bash
docker compose exec django python manage.py seed_uzorita_unit_bathrooms
```

| stay.hr `Unit.code` | Channex title | Channex room type UUID |
|---------------------|---------------|-------------------------|
| R1 | Luxury Room Uzorita - R1 | `e8fc8060-3df5-4e49-bee9-32903786b4ee` (staging) |
| R2 | Luxury Room Uzorita - R2 | `0d852a5e-41d5-4801-9bf1-679deabcfbec` |
| R6 | Luxury Room Uzorita - R6 | *(production UUID — see below)* |
| R3 | Luxury Room Uzorita - R3 | `6058e4da-0ed4-48a1-a877-fec38685589a` |
| R4 | Standard King Room R4 | `158d11e2-dcba-4c0d-b0aa-4d89073866ea` (production) |

Booking.com listing names may differ; map each channel listing to the matching Channex room type UUID in [staging channels](https://staging.channex.io/channels).

## Booking.com test hotel (certification channel)

| Field | Value |
|-------|--------|
| Channel ID | `8ee9c7aa-6433-4037-924b-4f95598782d5` (Demo tenant → Test Property - Stay.hr) |
| Booking hotel ID | `10745030` (GBP test account) |
| stay.hr property slug | `channex-bcom-test` |

| Booking room | Booking ID | stay.hr unit | Max guests | Adults | Children | Infants | Channex room type |
|--------------|------------|--------------|------------|--------|----------|---------|-------------------|
| Holiday Home | 1074503007 | `BCOM-HOLIDAY` | 11 | 11 | 0 | 0 | `430b1381-dace-44d6-8d5d-a0a1025819fc` |
| Studio | 1074503008 | `BCOM-STUDIO` | 2 | 2 | 0 | 0 | `18c437d7-13e3-4dbc-9565-48fad4832bf5` |

Seed:

```bash
docker compose run --rm django python manage.py seed_channex_booking_test_property --tenant-slug uzorita
docker compose run --rm django python manage.py seed_channex_booking_test_property --tenant-slug demo --also-demo
```

## Store mapping in stay.hr

```bash
# Set from Channex UI (Property + API key), then:
docker compose run --rm django python manage.py seed_uzorita_channex_config \
  --property-id YOUR_CHANNEX_PROPERTY_UUID \
  --api-key YOUR_STAGING_API_KEY
```

Config is saved as encrypted `IntegrationConfig` (`provider=channex`) for tenant `uzorita`.

Code: `backend/apps/integrations/channex/mapping.py`
