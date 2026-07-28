# WhatsApp setup (Meta Cloud API)

Stay.hr uses **Meta WhatsApp Cloud API** only (Graph API). There is no BSP/360dialog path.

## Webhook

- URL: `https://api.stay.hr/api/v1/integrations/whatsapp/webhook/`
- Verify token: `WHATSAPP_WEBHOOK_VERIFY_TOKEN` in `.env` (same value in Meta App → Webhooks)
- Signature: `WHATSAPP_APP_SECRET` — webhook HMAC verification is always enabled

## Credentials split

| Layer | What |
|-------|------|
| `.env` | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_WEBHOOK_VERIFY_TOKEN`, `WHATSAPP_API_VERSION`, optional `WHATSAPP_WABA_ID` |
| Database (`IntegrationConfig`) | `phone_number_id` (required), `display_phone_number` (UI/wa.me), optional `waba_id`, templates JSON |

`access_token` is **never** stored in the database or Django admin.

## Platform default number

Finestar platform WABA (`+385976615439`, `phone_number_id=1088787204326396`) lives on the system tenant `platform`:

```bash
docker compose exec django python manage.py migrate
docker compose exec django python manage.py seed_platform_whatsapp_config
docker compose restart django celery
```

Hotels without their own `IntegrationConfig` fall back to this number for outbound send.

## Hotel tenant with own number

```bash
export WHATSAPP_PHONE_NUMBER_ID='...'
export WHATSAPP_DISPLAY_PHONE_NUMBER='+385...'
export WHATSAPP_WABA_ID='...'   # optional, for template ops
docker compose exec django python manage.py seed_uzorita_whatsapp_config --tenant-slug uzorita
```

Token remains global `WHATSAPP_ACCESS_TOKEN` until multi-WABA credentials are added.

## Inbound routing (platform number)

1. Webhook creates `WhatsAppMessage` (audit on platform tenant)
2. `WhatsAppInboundRouting` record: thread → booking code → phone → unrouted/ambiguous
3. Unrouted inbox: Django admin or `GET/POST /api/v1/platform/whatsapp/unrouted/` (superuser)

## Deploy checklist

1. Remove legacy env: `WHATSAPP_PROVIDER`, `D360_API_KEY`, `D360_API_BASE_URL`, `WHATSAPP_API_SEND_V2`
2. Set Meta env vars in `.env`
3. `migrate` + `seed_platform_whatsapp_config`
4. Restart `django` + `celery`
5. Meta webhook: verify + subscribe `messages`
6. Test outbound from hotel without own config (platform fallback)
7. Test inbound from unknown guest → unrouted queue
8. Manual link via platform API → guest flow runs

## Template operations

Welcome name + Meta language are resolved together via `resolve_welcome_template` — see [ADR 0011](../architecture/adr/0011-whatsapp-welcome-template-resolution.md). Verify config maps:

```bash
docker compose exec django python manage.py verify_whatsapp_templates
docker compose exec django python manage.py merge_whatsapp_welcome_templates --dry-run
```

Require `waba_id` in config or `WHATSAPP_WABA_ID` in `.env` to create templates on Meta:

```bash
docker compose exec django python manage.py whatsapp_create_welcome_templates --tenant-slug platform
```
