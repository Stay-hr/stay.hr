# Invoice email capture

Capability: **prikupiti usable email za dostavu računa**. Izdavanje, fiskalizacija i slanje računa ostaju u postojećem checkout pipelineu (`perform_reservation_checkout` → `issue_guest_invoice` → `fiscalize` → `send_invoice_email_task`).

## Flow

1. Gost traži račun (invoice / facture / Rechnung / račun …).
2. Ako recipient nije usable (prazan ili OTA relay npr. `@guest.booking.com`) → auto-reply traži pravi email i postavlja `Reservation.invoice_email_waiting_at`.
3. Dok je waiting aktivan, inbound s **točno jednim** usable emailom → `update_invoice_email` (booker + primary guest) + potvrda.
4. Checkout šalje račun samo ako `is_usable_invoice_email(recipient)`.

## Pravila

| Pravilo | Ponašanje |
|---------|-----------|
| Overwrite | Auto-update samo dok je `WAITING_FOR_EMAIL`. Usable email van ciklusa se ne prepisuje (`invoice_email_not_requested`). |
| Više adresa | Ne bira se automatski; `invoice_email_ambiguous` + molba za jednu adresu. `invoice_email_received` samo za točno jedan usable email. |
| Timeout | `INVOICE_EMAIL_WAITING_TIMEOUT_DAYS` (default 14). |
| Recipient | `resolve_invoice_recipient` / `has_usable_invoice_recipient` biraju prvi usable (booker, zatim primary guest) — relay booker ne blokira usable guest email. |
| Channex | Revision ne smije prepisati usable email s OTA relay/praznim (`prefer_usable_invoice_email`). |

## Kod

| Modul | Uloga |
|-------|-------|
| `guest_email_quality.py` | `is_ota_relay_email`, `is_usable_invoice_email` |
| `invoice_email_capture.py` | waiting state + `update_invoice_email` |
| `guest_invoice_inbound.py` | Channex / WhatsApp / email hooks |
| `invoice_email.resolve_invoice_recipient` | skip relay |

Property flag: `guest_invoice_auto_reply_enabled`.

## Observability events

`invoice_email_requested`, `invoice_email_received`, `invoice_email_updated`, `invoice_email_ambiguous`, `invoice_email_not_requested`, `invoice_email_timeout`, `invoice_email_skipped_relay`, `invoice_sent`.
