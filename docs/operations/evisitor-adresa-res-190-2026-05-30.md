# eVisitor — pogrešan format adrese (rezervacija #190)

**Objekt:** Luxury Room Uzorita B&B (tenant #2)  
**Datum događaja:** 30. svibnja 2026.  
**Rezervacija:** #190 · booking `6574810586` · soba **R3** · 29.–31.5.2026.

---

## Sažetak

Ručni OCR i check-in za rezervaciju #190 prošli su uspješno, ali **eVisitor prijava za Snježanu Majstorović Ergotić (#1194) nije uspjela** jer je adresa gosta spremljena u krivom redoslijedu. eVisitor API odbio je `CityOfResidence: 'Dubrovačka 30'` jer to nije grad u njihovom šifrarniku.

Nakon ispravke adrese u format `Grad, ulica` prijava je prošla.

---

## Gosti

| Uloga | Guest ID | Ime | Dokument | eVisitor |
|-------|----------|-----|----------|----------|
| Primarni (ažuriran s Marko Ergotić) | #1194 | Snježana Majstorović Ergotić | HR osobna `117776670` | **potrebna** → prijavljena |
| Drugi gost | #1930 | Ena Ergotić (dijete, rođ. 5.9.2023.) | HR osobna `119799848` | nije potrebna (<18) |
| Booker (nepromijenjen) | — | Marko Ergotić | — | — |

**Occupancy:** `adults_count=1`, `children_count=1` (Booking je imao 2/0).

**Izvor fotki:** `.wp_photos/WhatsApp Image 2026-05-30 at 14.26.*.jpeg`  
**Media:** `data/media/id_documents/` prefiks `300526_`

---

## Kronologija

| Vrijeme (30.5.2026.) | Događaj |
|---------------------|---------|
| ~14:55 | OCR, ažuriranje gostiju #1194 i #1930, avatari, operativni check-in (`expected` → `checked_in`; dolazak 29.5., kasni check-in) |
| 12:56:31 | eVisitor login OK; country lookup HTTP 400 (upozorenje, ne blokira) |
| 12:56:32 | `CheckInTourist` HTTP **400** — greška na polju `CityOfResidence` |
| 12:56:41 | Ponovni pokušaj — ista greška |
| 12:57:47 | Adresa ispravljena; `CheckInTourist` HTTP **200** — status `sent` |

---

## Uzrok

Adresa je pri ručnom unosu spremljena kao:

```
Dubrovačka 30, Osijek
```

Mapper u [`backend/apps/integrations/evisitor/mapper.py`](../../backend/apps/integrations/evisitor/mapper.py) za `CityOfResidence` i `CityOfBirth` uzima **prvi segment prije zareza**:

```python
if guest.address and "," in guest.address:
    city_of_residence = guest.address.split(",")[0].strip()
```

Poslano u eVisitor:

```json
"CityOfResidence": "Dubrovačka 30"
```

eVisitor odgovor (iz `EvisitorSubmission` #86/#87):

```
[[[CityOfResidence]]] 'Dubrovačka 30' [[[ne postoji u sustavu.]]]
```

---

## Rješenje

Adresa oba gosta (#1194, #1930) promijenjena u:

```
Osijek, Dubrovačka 30
```

Ponovno slanje:

```python
from apps.integrations.evisitor.service import submit_guest_checkin
submit_guest_checkin(guest, force_retry=True)
```

**Rezultat:** Snježana `evisitor_status=sent`; sažetak rezervacije `complete`.

---

## Pravilo za budući ručni OCR

| ❌ Krivo | ✅ Ispravno |
|---------|------------|
| `Dubrovačka 30, Osijek` | `Osijek, Dubrovačka 30` |
| `STAPARSKI PUT 056, SOMBOR` | `SOMBOR, STAPARSKI PUT 056` |

Format **`Grad, ulica`** — usklađen s uspješnim prijavama u bazi (npr. `NOVI SAD, SUNČANI KEJ 037`, `SOMBOR, STAPARSKI PUT 056`).

HR osobna na stražnjoj strani piše `PREBIVALIŠTE: OSIJEK, OSIJEK` + ulica zasebno — pri mapiranju **grad ide prvi**.

**Ažuriranje (validacija):** street-first adrese (npr. `Dubrovačka 30, Osijek`) sada se **odbijaju lokalno** u [`validate_evisitor_residence_address`](../../backend/apps/integrations/evisitor/residence_address.py) prije eVisitor API-ja — nema automatskog okretanja redoslijeda. Vidi [evisitor.md — Check-in](../development/evisitor.md#check-in-u-evisitor).

**Ažuriranje (OCR, 2026-08):** Francuski ID OCR često emitira `ulica, POSTAL GRAD[, DRŽAVA]`. To **ne** mijenja validator. [`address_normalizer.py`](../../backend/apps/reservations/address_normalizer.py) (`source=ocr`, flag `OCR_ADDRESS_NORMALIZATION_ENABLED`) radi samo deterministički reorder kad postoje street + postal + city, i prihvaća kandidat **samo** ako prođe postojeći validator. Originalni OCR string ostaje u apply auditu (`ocr_address_original`). HR street-first bez postala i dalje ostaje odbijen (#190).

---

## Povezani dokumenti i kod

| Što | Gdje |
|-----|------|
| Ručni OCR runbook | [id-document-import.md](../development/id-document-import.md) |
| Residence address validator | `backend/apps/integrations/evisitor/residence_address.py` |
| OCR address normalizer | `backend/apps/reservations/address_normalizer.py` |
| eVisitor mapper | `backend/apps/integrations/evisitor/mapper.py` |
| eVisitor servis | `backend/apps/integrations/evisitor/service.py` |
| Logovi | `docker compose logs django` — tražiti `CheckInTourist`, `evisitor-submit`, guest ID `1194` |

---

## Napomene

- **Country lookup HTTP 400** u logu (`GET .../Country/`) pojavljuje se i kod uspješnih prijava — trenutno samo upozorenje; citizenship se fallback-om uzima iz ISO3 polja gosta (`HRV`).
- Kasni check-in (dolazak 29.5., prijava 30.5.) zahtijeva operativni shell; UI gumb „Prijavi gosta” ostaje onemogućen zbog `wrong_date` u `checkin.py`.
