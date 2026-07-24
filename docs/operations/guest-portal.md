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

1. Napravi / osiguraj `GuestPortalAccess` za rezervaciju (nakon dovršenog web check-ina task to radi automatski; ručno: ensure u shellu ili PR-D „Pošalji portal link”).
2. Otvori: `https://booking.uzorita.hr/g/{token}` (opcionalno `?lang=hr`).
3. Javni API: `GET /api/v1/public/guest-portal/{token}/`

Key guide dry-run (bez slanja):

```bash
docker compose --profile test-run run --rm django-run \
  python manage.py compose_key_handover_guide --reservation-id N
```

---

## Distribucija linka nakon web check-ina

Nakon uspješnog `complete_session`, Celery task `reservations.send_guest_portal_link_after_checkin` šalje na **isti kanal** kojim je pokrenut check-in (`GuestCheckInSession.created_from`):

| `created_from` | Kanal | Oblik |
|----------------|-------|-------|
| `channex` | Channex / Booking | **Dvije poruke:** (1) CTA + potpis + footer, (2) samo URL s `?lang=` |
| `whatsapp_autocheckin` | WhatsApp | Isto (dvije poruke) |
| `email` | Email | Jedna HTML poruka (CTA + tipka) |
| `reception_manual` | Email ako postoji, inače skip | Kao email |

- Dedup hint: `guest_portal_link` (jednom po rezervaciji; CTA/prva poruka). Druga BOOKING/WA poruka: hint `guest_portal_link url`.
- Email/Channex gosti **ne** dobivaju WhatsApp portal link.
- Meta welcome template se ne mijenja.

Ručno slanje s recepcije: Property Settings → Share (PR-D2; [ADR 0008](../architecture/adr/0008-property-settings.md)).

---

## Uređivanje sadržaja

Svakodnevne izmjene WiFi / dolazak / parking / doručak / kontakt / self-service: **app.stay.hr/settings** (Property Settings — [ADR 0008](../architecture/adr/0008-property-settings.md)). Guest portal (`/g/{token}`) je samo view.

ADR: [0007-guest-portal.md](../architecture/adr/0007-guest-portal.md) · [0008-property-settings.md](../architecture/adr/0008-property-settings.md).
