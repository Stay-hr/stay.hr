# Messaging Orchestration — LIVE rollout checklist

Operativna procedura za **LIVE** cutover Messaging Enginea (ADR 0010) — dio **Rollout** traga (post-v1).  
Nije kod — checklist za onoga tko radi rollout (i za handover).

**Post-v1 kontekst:** [messaging-orchestration-post-v1.md](messaging-orchestration-post-v1.md) (Rollout vs Adoption; nema Phase 9 jezgre)  
**Scope v1:** `CHECKIN_INFO`, `CHECKIN_LINK`, `WELCOME` (WhatsApp)  
**Allowlist (primjer):** `MESSAGE_ORCHESTRATION_TENANTS=uzorita`  
**Kod / flagovi:** `apps.communications.messaging.flags`, Celery `communications.run_message_orchestration`  
**ADR:** [0010-messaging-orchestration-engine.md](../architecture/adr/0010-messaging-orchestration-engine.md)

---

## Pravilo

| Faza | `ENABLED` | `SHADOW` | Legacy reminders / autocheckin | Guest-visible engine send |
|------|-----------|----------|--------------------------------|---------------------------|
| Off | `false` | * | ON | Ne |
| Shadow | `true` | `true` | ON | Ne (samo planned outbox) |
| Live + dual | `true` | `false` | ON (suppression OFF) | Da — **mogući double-send** |
| Live cutover | `true` | `false` | OFF (suppression ON) | Da — engine only |

**Ne preskači Shadow.** Planned mora rasti prije prvog provider senda.

**Canary:** prvi LIVE drži uzak allowlist (`TENANTS` ili jedan `PROPERTIES` token). Ne otvaraj sve tenante odjednom.

**Dual-send prozor:** dok je engine LIVE a legacy suppression još OFF, gost može dobiti i stari i novi path. Drži taj prozor **kratkim** (smoke → 24h max na canaryju) ili odmah prijeđi na suppression ON čim Delivered/Fallback/Alert checklist prođu.

Nakon `.env` izmjene: `docker compose up -d django celery-worker celery-beat` (ne `restart` — env se ne osvježava).

---

## 0. Priprema (prije Shadow)

```env
MESSAGE_ORCHESTRATION_ENABLED=true
MESSAGE_ORCHESTRATION_SHADOW=true
MESSAGE_ORCHESTRATION_TENANTS=uzorita
MESSAGE_ORCHESTRATION_PROPERTIES=
OPERATIONS_ALERT_EMAILS=…   # mora biti postavljen prije LIVE
# Stop legacy midnight WA D0 until LIVE+suppress (ADR 0010 §11):
GUEST_CHECKIN_REMINDER_DAYS_BEFORE=7
```

□ Beat schedule ima `message-orchestration` (~15 min)  
□ `django` + `celery-worker` + `celery-beat` recreatirani s novim env  
□ Ops: `python manage.py run_message_scheduler --materialize-only --property-id <ID> --show-planned` radi (bypass flagova — ops probe)

---

## 1. Shadow — outbox bez senda

Cilj: scheduler i materializacija na pravim podacima, **bez** provider I/O.

```env
MESSAGE_ORCHESTRATION_ENABLED=true
MESSAGE_ORCHESTRATION_SHADOW=true
```

| Check | Kako |
|-------|------|
| □ Scheduler radi | Log: `message_orchestration_done mode=shadow` ili `messaging_scheduler_cycle` svakih ~15 min |
| □ Planned raste | `MessageDispatch` sa `status=planned` za allowlisted tenant/property; `--show-planned` ili shell count |

```bash
docker compose --profile test-run run --rm django-run python manage.py shell -c "
from django.conf import settings
from apps.communications.messaging.models import MessageDispatch, MessageDispatchStatus
from apps.communications.messaging.flags import flags_health_snapshot
print('flags', flags_health_snapshot())
print('planned', MessageDispatch.objects.filter(status=MessageDispatchStatus.PLANNED, archived_at__isnull=True).count())
print('queued', MessageDispatch.objects.filter(status=MessageDispatchStatus.QUEUED, archived_at__isnull=True).count())
"
```

□ Nema neočekivanih `failed` / spam alerta (u shadowu ne bi trebalo biti provider sendova)  
□ Legacy reminder + WhatsApp autocheckin i dalje šalju (očekivano)

**Ne idi na LIVE dok Planned ne raste na canaryju.**

---

## 2. Prvi LIVE (suppression još OFF)

Cilj: potvrditi claim → deliver → fallback → alert path. Legacy još aktivan.

```env
MESSAGE_ORCHESTRATION_ENABLED=true
MESSAGE_ORCHESTRATION_SHADOW=false
# TENANTS / PROPERTIES ostaju canary
```

Recreate containers. Zatim:

| Check | Kako |
|-------|------|
| □ Claimed raste | Log: `messaging_metric name=messaging_dispatch_claimed` / `message_orchestration_done … dispatched=` |
| □ Delivered postoji | `MessageDispatch.status=delivered` + `MessageDeliveryAttempt.success=True`; timeline `GuestOutboundMessage` |
| □ Fallback normalan | Booking → email (CHECKIN_*): ako booking faila, email attempt postoji; isti `render_checksum`; `fallback_used=True` kad treba |
| □ Alert rate normalan | `messaging_all_providers_failed` rijedak; throttle OK; `OPERATIONS_ALERT_EMAILS` prima samo prave outage-e |
| □ Legacy suppression OFF | Stari `GuestReminderService` / `run_whatsapp_autocheckin_welcome` još rade — **svjesno** (dual-send rizik na canaryju) |

Brzi status:

```bash
docker compose --profile test-run run --rm django-run python manage.py shell -c "
from django.db.models import Count
from apps.communications.messaging.models import MessageDispatch, MessageDeliveryAttempt
print(dict(MessageDispatch.objects.values_list('status').annotate(c=Count('id'))))
print('attempts_ok', MessageDeliveryAttempt.objects.filter(success=True).count())
print('attempts_fail', MessageDeliveryAttempt.objects.filter(success=False).count())
print('fallback_used', MessageDispatch.objects.filter(fallback_used=True).count())
"
```

Logovi (worker):

```bash
docker compose logs --since 2h celery-worker 2>&1 | rg 'message_orchestration_|messaging_metric|messaging_all_providers_failed'
```

---

## 3. 24h promatranje (canary)

Dok je LIVE + legacy suppression OFF (ili već ON — ovisi o dual-send toleranciji):

□ Nema rasta `failed` bez razloga  
□ Nema alert storma (`messaging_all_providers_failed` / ops mail)  
□ Planned/queued depth ne “zapinje” (claimed i delivered prate due_at)  
□ WELCOME / CHECKIN_* sadržaj guest-visible OK na uzorku rezervacija  
□ Idempotency: nema duplih engine dispatcheva za isti definition×reservation (dedupe)  
□ Dual-send: ako legacy još ON — zabilježi koliko se poklapa; planiraj suppression

Ako bilo što crveno → **Rollback** (dolje), ne pali suppression.

---

## 4. Legacy suppression ON (cutover)

Tek kad gornji checklisti prođu.

Phase 7 wiring (**shipped**): `suppress_legacy_automated_outbound(...)` u:

- `GuestReminderService` (pre-arrival + D0)
- `run_whatsapp_autocheckin_welcome` / intro email / immediate welcome
- `send_whatsapp_autocheckin_welcome` manage (bypass: `--force`)

Suppression je **automatski ON** čim je `ENABLED=true`, `SHADOW=false`, i scope na allowlisti — nema zasebnog flaga.

Prije canary LIVE cutovera potvrdi satove:

```bash
docker compose --profile test-run run --rm django-run \
  python manage.py align_messaging_schedules --tenant-slug uzorita --property-slug uzorita --dry-run
# zatim bez --dry-run
```

Očekivano: pre-arrival **7d @ 09:00 FIXED_TIME**, WhatsApp welcome **0d @ 11:15 FIXED_TIME**.

□ Suppression aktivna za canary allowlist (live, not shadow)  
□ Potvrdi: legacy task **ne** šalje CHECKIN_*/WELCOME za taj tenant/property  
□ Engine i dalje Delivered  
□ 24–48h nakon cutovera: isti health checkovi kao u §3, bez dual-send

---

## 5. Proširenje allowliste

Tek nakon stabilnog canaryja:

1. Dodaj tenant/property u `MESSAGE_ORCHESTRATION_TENANTS` / `_PROPERTIES`  
2. Recreate django + celery-worker + celery-beat  
3. Ponovi kratki Shadow→LIVE smoke na novom scopeu (ne mora punih 24h ako je isti stack)  
4. Suppression ON za novi scope

---

## Rollback

**Odmah (zaustavi engine send):**

```env
MESSAGE_ORCHESTRATION_ENABLED=false
# ili
MESSAGE_ORCHESTRATION_SHADOW=true
```

Zatim `docker compose up -d django celery-worker celery-beat`.

| Situacija | Akcija |
|-----------|--------|
| Sumnja u deliver/fallback/alert | `SHADOW=true` ili `ENABLED=false`; legacy ostaje ON |
| Suppression već ON, treba legacy natrag | Isključi suppression (Phase 7 invert) **i** `ENABLED=false` / `SHADOW=true` |
| Zapeli `dispatching` redovi | Ops review; ne hard-delete — soft cancel / replay po runbooku kasnije |

Planned redovi u outboxu smiju ostati (audit); soft-delete only (`archived_at`), nikad hard-delete.

---

## One-page tok

```text
□ Shadow: scheduler radi
□ Shadow: planned raste
        ↓
□ LIVE: claimed raste
□ LIVE: delivered postoji
□ LIVE: fallback normalan
□ LIVE: alert rate normalan
□ Legacy suppression OFF   ← dual-send moguć; canary only
        ↓
   24h promatranje
        ↓
□ Legacy suppression ON    ← engine only za allowlist
        ↓
   proširi allowlist po potrebi
```

---

## Reference

- ADR [0010](../architecture/adr/0010-messaging-orchestration-engine.md) — flagovi, ban na direct sendere; post-v1 = Rollout + Adoption  
- Post-v1 tracks: [messaging-orchestration-post-v1.md](messaging-orchestration-post-v1.md)  
- ADR [0004](../architecture/adr/0004-guest-checkin-session.md) — session SoT; engine samo distribuira  
- Ops probe: `manage.py run_message_scheduler --materialize-only --show-planned`  
- Metrics log line: `messaging_metric name=…`  
- Alert log line: `messaging_all_providers_failed`
