# No-show — Flutter (Hospira) upute

Backend (svibanj 2026) podržava status **`no_show`** („Nije došao”) i za Channex/Booking.com rezervacije automatski šalje prijavu na kanal preko Channex Reporting API-ja.

**Repo:** [github.com/Stay-hr/hospira_flutter](https://github.com/Stay-hr/hospira_flutter) (`hr.finestar.hospira`)

---

## Kada prikazati akciju

| Uvjet | Akcija |
|-------|--------|
| `reservation.status == "expected"` | Prikaži **Nije došao** |
| bilo koji drugi status | Sakrij akciju |

Dozvoljeni prijelaz: **`expected` → `no_show`** (terminalni status — nema daljnjih promjena).

---

## API: označi no-show

```http
PATCH /api/v1/reception/reservations/{reservationId}/
Authorization: Bearer {api_token}
Content-Type: application/json

{
  "status": "no_show",
  "waived_fees": true
}
```

### Polja

| Polje | Tip | Obavezno | Opis |
|-------|-----|----------|------|
| `status` | string | da | Mora biti `"no_show"` |
| `waived_fees` | boolean | ne | Default `true`. Samo relevantno za Channex rezervacije |

### `waived_fees`

| Vrijednost | Značenje na Booking.com |
|------------|-------------------------|
| `true` | Oprosti no-show naknadu gostu; Booking.com obično ne naplaćuje proviziju |
| `false` | Naplati gostu prema no-show politici; Booking.com naplaćuje proviziju i od naknade |

Za **ručne** rezervacije (`import_source != "channex"`) polje se ignorira — mijenja se samo lokalni status.

---

## Channex / Booking.com

Backend poziva Channex samo kad:

- `import_source == "channex"`, i
- `external_id` počinje s `channex:` (validan Channex booking ID).

Redoslijed:

1. `POST https://app.channex.io/api/v1/bookings/{booking_id}/no_show` s `waived_fees`
2. Ako Channex vrati grešku → **status se ne mijenja** u bazi
3. Ako uspije → `status = no_show`, `booking_status = "no_show"`

**Prozor za prijavu:** od ponoći na dan dolaska (lokalno vrijeme objekta) do **48 sati** nakon check-ina. Izvan prozora Channex može vratiti 422 — prikaži poruku korisniku.

---

## Odgovori

### Uspjeh (200)

Vraća puni detail rezervacije (isti oblik kao `GET .../reservations/{id}/`):

```json
{
  "id": 789,
  "status": "no_show",
  "import_source": "channex",
  "booking_status": "no_show",
  "...": "..."
}
```

Nakon uspjeha osvježi detail i listu (ili koristi `sync-versions` hash ako je implementiran).

### Nedozvoljen prijelaz (400)

```json
{
  "status": ["Nedozvoljen prijelaz statusa rezervacije."]
}
```

Npr. pokušaj `checked_in` → `no_show`.

### Channex greška (400)

```json
{
  "status": ["Channex POST /bookings/.../no_show failed (422): ..."]
}
```

Prikaži `status` string korisniku; rezervacija ostaje `expected`.

---

## UI preporuka (tablet)

### 1. Potvrda no-showa

```
Označiti rezervaciju kao no-show?
[Odustani] [Nastavi]
```

### 2. Izbor naknade (samo Channex)

Ako `import_source == "channex"`:

```
Oprostiti no-show naknadu gostu?
[Oprosti naknadu]  [Naplati gostu]  [Odustani]
```

Mapiranje:

- **Oprosti naknadu** → `waived_fees: true`
- **Naplati gostu** → `waived_fees: false`

Za manual/PDF rezervacije preskoči korak 2.

### 3. Prikaz statusa

| `status` | HR label | Boja (primjer) |
|----------|----------|----------------|
| `no_show` | Nije došao | narančasta / upozorenje |

---

## Dart helper (primjer)

```dart
enum NoShowFeeChoice { waive, charge }

Future<ReservationDetail> markNoShow({
  required int reservationId,
  required NoShowFeeChoice feeChoice,
}) async {
  final res = await api.patch(
    '/reception/reservations/$reservationId/',
    body: {
      'status': 'no_show',
      'waived_fees': feeChoice == NoShowFeeChoice.waive,
    },
  );

  if (res.statusCode == 200) {
    return ReservationDetail.fromJson(jsonDecode(res.body));
  }

  final body = jsonDecode(res.body) as Map<String, dynamic>;
  final statusErr = body['status'];
  final message = statusErr is List
      ? statusErr.first.toString()
      : statusErr?.toString() ?? body['detail']?.toString() ?? 'Promjena statusa nije uspjela.';
  throw ApiException(res.statusCode, message);
}
```

### Kada prikazati gumb

```dart
bool canMarkNoShow(ReservationDetail r) => r.status == 'expected';

bool needsFeeChoice(ReservationDetail r) => r.importSource == 'channex';
```

---

## Razlika: no-show vs otkaz

| | **Otkaz** (`canceled`) | **No-show** (`no_show`) |
|--|------------------------|-------------------------|
| Gost nije došao | Da (ručno) | Da (Booking.com terminologija) |
| Channex Reporting API | Ne | Da (za `import_source: channex`) |
| Booking.com extranet | Ručno / gost otkazao | Mark as no-show |
| Soba se oslobađa | Da | Da |

Za Booking.com kanal preferiraj **no-show** kad gost jednostavno nije došao na dolazak — ne **otkaz** — jer se time ispravno prijavljuje kanalu i provizija se obračunava prema pravilima Booking.coma.

---

## Deploy ovisnosti

Potreban backend s migracijom `reservations.0012_reservation_status_no_show` i novim kodom:

```bash
docker compose exec django python manage.py migrate
docker compose build django && docker compose up -d django
```

Stari Flutter app bez no-show gumba i dalje radi; nema breaking change na postojećim endpointima.

---

## Povezano

- [channex-uzorita-booking-channel.md](../integrations/channex-uzorita-booking-channel.md) — Channex prava (no-show, commission waiver)
- [evisitor.md](evisitor.md) — eVisitor arhitektura, statusi, checkout
- [evisitor-flutter.md](evisitor-flutter.md) — eVisitor submit iz Hospire
- [Channex Bookings API — No Show Report](https://docs.channex.io/api-v.1-documentation/bookings-collection)
