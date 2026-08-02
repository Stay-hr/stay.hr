# WhatsApp Business App coexistence — follow-ups

Backlog after `smb_message_echoes` → stay.hr timeline. Ops setup: [whatsapp-setup.md](../operations/whatsapp-setup.md).

Do **not** expand the coexistence PR with these; track separately.

## P1 (recommended)

### 1. Replay webhook manage command

```bash
python manage.py replay_whatsapp_webhook <file.json>
# and/or
python manage.py replay_whatsapp_echo <wamid>
```

Speeds up debugging without Meta Dashboard (replay from file or stored `raw_payload`).

### 2. Health / diagnostics

Small diagnostics output for support, e.g.:

- Business App echoes enabled
- Last `business_app` echo timestamp
- Last `cloud_api` outbound timestamp

Candidate: extend reception `GET /system/status` or a narrow ops endpoint.

### 3. Counters (no Prometheus required yet)

Log or simple metrics:

- `business_app_echo_total`
- `business_app_echo_duplicates`
- `business_app_echo_orphan`
- `business_app_echo_matched_thread`
- `business_app_echo_matched_phone`

## P2

### 4. Lazy media download

`media_id` is preserved in echo `raw_payload`. Open TODO: download media lazily when needed. Do not implement in the coexistence PR.

### 5. Unknown SMB echo type alert

When Meta adds types (`interactive`, `reaction`, `poll`, …), log/alert:

```
Unknown SMB echo type detected
```

Parser already stores unknown types without failing; this is observability only.

## P3

### 6. ADR-0017 — WhatsApp Business App coexistence

One-page ADR covering:

- why `WhatsAppMessage.source` exists (`cloud_api` vs `business_app`)
- why echoes do not create `GuestOutboundMessage`
- why the reception timeline reads `WhatsAppMessage`
- why an echo is outbound audit, not inbound (no `process_inbound_message`)

## Ops (separate from feature)

### 7. PostgreSQL table ownership for Django migrations

On 2026-08-02 deploy, `integrations.0027` failed as role `stay` (`must be owner of table integrations_whatsappmessage`; owner was `postgres`). Workaround: DDL as `postgres` + insert into `django_migrations`.

**Task:** one-time standardize ownership (or grants) so future `migrate` as `stay` works without manual `django_migrations` inserts. Not a blocker for the coexistence feature merge.
