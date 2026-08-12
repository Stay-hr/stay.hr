# Guest portal — Uzorita ops

Operativni runbook za guest portal (`/g/{token}`) na tenantu **Uzorita** (#2).

---

## Seed

```bash
docker compose --profile test-run run --rm django-run \
  python manage.py seed_uzorita_guest_info
```

Postavlja `Property.guest_info` (wifi, arrival, parking, breakfast, key guide) te:

| Polje | Vrijednost |
|-------|------------|
| `self_service_mode` | `schedule` |
| `self_service_config` | `{"weekdays": [1]}` (utorak, Python weekday) |

Kartica **key guide** na portalu vidljiva je samo kad je check-in datum utorak.

Dry-run:

```bash
docker compose --profile test-run run --rm django-run \
  python manage.py seed_uzorita_guest_info --dry-run
```

---

## Test URL

1. Napravi / osiguraj `GuestPortalAccess` za rezervaciju (nakon dovršenog web check-ina task to radi automatski; ručno: ensure u shellu ili Property Settings → Share).
2. Otvori: `https://booking.uzorita.hr/g/{token}` (opcionalno `?lang=hr`).
3. Javni API: `GET /api/v1/public/guest-portal/{token}/`

Key guide dry-run (bez slanja):

```bash
docker compose --profile test-run run --rm django-run \
  python manage.py compose_key_handover_guide --reservation-id N
```

---

## Distribucija linka nakon web check-ina

Nakon uspješnog `complete_session`, Celery task `reservations.send_guest_portal_link_after_checkin` šalje portal link, zatim (ako treba) pitanje o vremenu dolaska.

Kanal za portal pokušaj: `last_distributed_from || created_from` (G1: `last_distributed_from` se postavlja **samo** nakon uspješnog check-in link senda).

| `created_from` | Kanal | Oblik |
|----------------|-------|-------|
| `channex` | Channex / Booking | **Jedna** poruka: CTA + `/g/{token}?lang=` + potpis |
| `whatsapp_autocheckin` | WhatsApp | Isto |
| `email` | Email | Plain `body_text` s URL-om; HTML tipka zadržana |
| `reception_manual` | Email ako postoji, inače skip | Kao email |

### Guardraili (G2–G8)

- **G2:** arrival ask samo nakon portal `sent`.
- **G3:** portal dedup = uspješan outbound za session + **aktualni** token + channel; `failed` ne blokira; `allow_resend` bypass.
- **G4:** ask dedup success-only, channel-agnostic (nema `allow_resend` na ask).
- **G5:** atomic claim (`PostCheckinSendClaim`): `pending`/`sent` blokiraju, `failed` reclaim; provider I/O izvan transakcije.
- **G6–G7:** ask kanal = najnoviji **uspješni** portal outbound za session+token; failed resend ne krade ownership.
- **G8:** nakon regenerate, stari token ne hrani sticky/ask; čeka se uspješan portal za novi token.

Dedup / claim hintovi: `guest_portal_link`; claim keys `guest_portal:…`, `arrival_ask:…`.

- Email/Channex gosti **ne** dobivaju WhatsApp portal link osim ako je to bio uspješni distribution kanal.
- Meta welcome template se ne mijenja.
- Reception timeline: plain URL u `body_text`; badge **Failed** / **Neuspjelo** kad je outbound `status=failed`.

Ručno slanje s recepcije: Property Settings → Share ([ADR 0008](../architecture/adr/0008-property-settings.md)).

---

## Uređivanje sadržaja

Svakodnevne izmjene WiFi / dolazak / parking / doručak / kontakt / self-service: **app.stay.hr/settings** (Property Settings — [ADR 0008](../architecture/adr/0008-property-settings.md)). Guest portal (`/g/{token}`) je samo view.

ADR: [0007-guest-portal.md](../architecture/adr/0007-guest-portal.md) · [0008-property-settings.md](../architecture/adr/0008-property-settings.md).
