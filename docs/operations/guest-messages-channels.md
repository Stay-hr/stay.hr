# Guest messages — tri kanala (Mail, Channex, WhatsApp)

Operativni runbook za slanje i primanje poruka gostima iz stay.hr recepcije (web + Flutter).

---

## Kanali

| Kanal | Outbound | Inbound u stay.hr | Rezervacije |
|-------|----------|-------------------|-------------|
| **Mail** | Tenant SMTP (`room_reservations@uzorita.hr`) | Da (IMAP poll, Booking.com `@guest.booking.com`) | Sve s emailom |
| **Channex** | Channex Messages API → B.com Poruke | Da (webhook `message`) | `import_source=channex` |
| **WhatsApp** | Handoff (`wa.me`) | Da (WhatsApp webhook) | S telefonom |

**Vlastita booking platforma** (stay.hr web booking, `source=api`): nema guest app — koristi **Mail** (+ WhatsApp). Inbound odgovori gosta na mail s `@guest.booking.com` ulaze u chat timeline (IMAP poll svake 2 min).

**B.com PDF import** (`import_source=booking_pdf`): Mail na `@guest.booking.com` + WhatsApp; Channex API **nije** dostupan (nema Channex linka).

---

## Channex setup (jednokratno)

1. Channex UI → Property → Apps → **Messages & Reviews** — instalirano
2. Webhook (isti URL kao bookingi):
   ```text
   https://api.stay.hr/api/v1/integrations/channex/webhook/?provider=stay&env=staging
   ```
   Header: `X-Stay-Channex-Webhook: <secret iz IntegrationConfig>`
3. Dodati event **`message`** s **`send_data=true`**

---

## API (recepcija)

| Metoda | Ruta |
|--------|------|
| GET | `/api/v1/reception/message-threads/` — inbox (kartice po rezervaciji) |
| GET | `/api/v1/reception/reservations/{id}/messages/` — unified chat timeline |
| POST | `/api/v1/reception/reservations/{id}/messages/compose/` |
| POST | `/api/v1/reception/reservations/{id}/messages/send/` — body: `{ draft_id, channel, body_text }` |
| GET | `/api/v1/reception/reservations/{id}/channex-messages/` — samo Channex (legacy; DB-only GET) |

Timeline and inbox GET are **DB-only** ([ADR 0019](../architecture/adr/0019-messaging-conversation-store.md) Phase A). Query `?sync=1` / `auto` is ignored and does **not** pull Channex or poll IMAP. Ingest is webhook + Celery (`poll_guest_email_inbox`, `sync_channex_messages_for_upcoming_checkins`).

Automatic Channex reconcile (every 15 min, Uzorita) is **A ∪ B ∪ C ∪ D** after an Eligible filter (`import_source=channex`, can sync, status not canceled/no_show/refused/pending). D (recent `ChannexMessage`) cannot re-admit excluded statuses. CLI `sync_channex_booking_messages` remains the one-off backfill / incident escape hatch — not GET.

Channex attachments are downloaded at ingest (webhook + reconcile). `GET …/channex-messages/{id}/media/` serves local `media_file` only; missing file is **404**, never a live Channex fetch.

`channel` u send: `email` | `booking` | `whatsapp`

---

## Reception web

Detalj rezervacije → sekcija **Poruke gostu** ([`GuestMessagesPanel.tsx`](../../web/reception/app/_components/GuestMessagesPanel.tsx)):

- Chat timeline
- Generiraj (check-in / odgovor / prilagođeno)
- Odabir kanala: Mail / WhatsApp / Channex (samo dostupni); **Mail** je zadani kad je dostupan

---

## CLI

```bash
# Poll IMAP inbox za inbound Booking.com mailove
docker compose exec django python manage.py poll_guest_email --tenant=uzorita

# Pošalji poruku preko Channexa
docker compose exec django python manage.py send_channex_booking_message \
  --reservation-id 798 \
  --message-file docs/operations/booking-message-5238895494-pierre-fr.txt \
  --tenant-slug uzorita

# Povuci postojeće poruke iz Channexa (backfill)
docker compose exec django python manage.py sync_channex_booking_messages \
  --tenant-slug uzorita

# Jedna rezervacija
docker compose exec django python manage.py sync_channex_booking_messages \
  --tenant-slug uzorita --reservation-id 798
```

---

## Provjera

| Test | Očekivano |
|------|-----------|
| Gost piše na B.com (email) | Inbound u timeline (IMAP poll); FCM push |
| Gost piše na B.com (Channex) | Inbound u admin → Channex messages; vidljivo u web/Flutter timeline |
| Send `booking` | Poruka u B.com extranet Poruke |
| Send `email` | Mail na gostovu adresu; Sent u webmail |
| Send `whatsapp` | Otvara WhatsApp s predloženim tekstom |
| Vlastita platforma compose | `booking.available=false`, `email.available=true` |

---

## Troubleshooting

| Simptom | Rješenje |
|---------|----------|
| Send booking → 403 | Messages & Reviews app nije aktivan na Channex propertyju |
| Inbound mail ne stiže | Provjeri `guest_imap_enabled`, SMTP lozinku, `poll_guest_email --tenant=uzorita`; mail mora biti s `@guest.booking.com` |
| Poruka u Pulseu/mailu, ne u messengeru | Mail/Pulse nisu ingest kanal — samo Channex webhook/API. Provjeri `external_id` (`channex:uuid` vs samo booking code), CLI `sync_channex_booking_messages --reservation-id` (ne UI `sync=1`), Channex Messages & Reviews app |
| `booking.available=false` | Rezervacija nije `import_source=channex` ili nema Channex linka (UUID, booking code ili revision) |
| Mail ne odlazi | Tenant SMTP u Reception settings (`guest_contact_email` + password) |

---

## Povezano

- [ADR 0019 — Conversation store](../architecture/adr/0019-messaging-conversation-store.md)
- [guest-messages-flutter.md](../development/guest-messages-flutter.md) — Flutter implementacija
- [channex-uzorita-booking-channel.md](../integrations/channex-uzorita-booking-channel.md)
- [whatsapp-checkin-template.md](./whatsapp-checkin-template.md)
