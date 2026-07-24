# Channex — što može uređivati na Booking.com

Snapshot iz Booking.com extraneta **„What can Channex.io offer?“** (2026-05-26) za objekt Luxury Room Uzorita B&B (hotel ID `4181954`).

Izvorno: [channex-uzorita-booking-channel.md](channex-uzorita-booking-channel.md).

**Napomena Booking.com:** *Not all providers offer the full functionality associated with a connection type.*

---

## Odobrena prava (Booking.com extranet)

| Kategorija | Tko smije (Booking pravilo) | Channex |
|------------|----------------------------|---------|
| Rates and availability | One provider | ✅ odobreno |
| Reservations | One provider | ✅ odobreno |
| Guest reviews | Multiple providers | ✅ odobreno |
| Reporting | Multiple providers | ✅ odobreno |
| Content | One provider | ✅ odobreno |
| Photos | One provider | ✅ odobreno |
| Guest messages | One provider | ✅ odobreno |
| Performance data and insights | Multiple providers | ✅ odobreno |

---

## Rezervacije

| Funkcija | Channex |
|----------|---------|
| Pregled i ažuriranje rezervacija / otkazivanja | ✅ |
| Prijava nevažeće kartice | ✅ |
| Prijava no-show | ✅ |
| No-show commission waiver | ✅ |
| Prijava promjene boravka (stay changes) | ❌ (nije u ponudi) |
| Otkaz zbog nevažeće kartice | ❌ |

## Rates and availability (cijene i raspoloživost)

| Funkcija | Channex |
|----------|---------|
| Inventar soba (room inventory) | ✅ |
| Restrikcije (min stay, stop sell, CTA/CTD, …) | ✅ |
| Standard pricing | ✅ |
| Single occupancy pricing | ✅ |
| Occupancy-based pricing (OBP) | ✅ |
| Pregled zadnjeg inventara/cijena na Booking.com | ✅ |
| Length-of-stay pricing | ❌ |
| Derived pricing | ❌ |

## Content (sadržaj objekta)

| Funkcija | Channex |
|----------|---------|
| Dodavanje property/listinga | ✅ |
| Facilities | ✅ |
| Kontakt objekta | ✅ |
| Policies | ✅ |
| Fotografije | ✅ |
| House rules | ❌ |

## Room and rate management

| Funkcija | Channex |
|----------|---------|
| Kreiranje / ažuriranje soba | ✅ |
| Kreiranje / ažuriranje rate planova | ✅ |
| Dodjela rate planova sobama | ✅ |
| Pregled svih soba i cijena | ✅ |

## Promotions

| Funkcija | Channex |
|----------|---------|
| Kreiranje promocija | ✅ |
| Ažuriranje / deaktivacija | ✅ |
| Performance promocija | ✅ |

Budući stay.hr path: [ADR 0009](../architecture/adr/0009-channel-promotions.md) — Phase 0 closed, **`BookingPromotionProvider`** (Channex nema Promotions API). Danas: Booking Extranet (± Channex UI). Probe: [channex-promotions-api-probe.md](../operations/channex-promotions-api-probe.md).

## Guest reviews

| Funkcija | Channex |
|----------|---------|
| Odgovor na recenziju | ✅ |
| Pregled ocjena | ✅ |
| Pregled recenzija | ✅ |

## Guest messaging

| Funkcija | Channex |
|----------|---------|
| Slanje poruke gostu | ✅ |
| Dohvat jednog razgovora | ✅ |
| Upload privitka u razgovor | ✅ |
| Poruka s privitkom | ✅ |
| Dohvat svih razgovora po propertyju | ❌ |
