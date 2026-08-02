# Smoke evidence: `smb_message_echoes`

| Field | Value |
|-------|-------|
| Date | 2026-08-02 |
| Base commit (main at smoke time) | `2205fdca7b9b11f333330b3ced1b2a08429bb0a0` |
| Feature | WhatsApp Business App coexistence echoes → stay.hr timeline |
| Environment | Production stack (`stay_django` + PostGIS) |
| Method | Unit tests (`test_whatsapp_smb_echoes`) + production smoke via `process_whatsapp_webhook` with Meta-shaped payloads; Cloud API checked against existing live outbound rows |

## Result: PASS

| Check | Status | Notes |
|-------|--------|-------|
| Cloud API | PASS | Existing outbound `source=cloud_api`; timeline `whatsapp_source=cloud_api` |
| Business App | PASS | Simulated `smb_message_echoes` → `OUTBOUND` + `source=business_app`; reservation linked (`matched_by=thread`) |
| Redelivery | PASS | Same `wamid` → `duplicate`; no second row; no version bump |
| Orphan | PASS | Unknown `to` → `reservation_id=NULL`, `matched_by=none`, no version bump |
| Coexistence | PASS | Same reservation has both `cloud_api` and `business_app` without duplicate `wamid` |

## Pending (ops, not code)

Live E2E with a real WhatsApp Business App reply after Meta Coexistence onboarding (confirm Meta delivers `smb_message_echoes` in production).

## Deploy note (ops follow-up)

Migration `0027_whatsappmessage_source_received_at` could not be applied by DB role `stay` because `integrations_whatsappmessage` was owned by `postgres`. Columns were added as `postgres` and the migration row recorded in `django_migrations`. See ops task in [whatsapp-coexistence-followups.md](../development/whatsapp-coexistence-followups.md) (table ownership).
