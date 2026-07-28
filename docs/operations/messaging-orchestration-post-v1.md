# Messaging Orchestration — post-v1 (Rollout & Adoption)

After [ADR 0010](../architecture/adr/0010-messaging-orchestration-engine.md) Phases **1–8**, the Messaging Engine **v1 core is frozen**. There is **no Phase 9** that redesigns the engine.

Further work has exactly two categories:

| Track | Goal | Changes core? |
|-------|------|---------------|
| **Rollout** | Operate and expand live CHECKIN_* / WELCOME | No |
| **Adoption** | Move more product flows onto the same engine | No (definitions/triggers/providers only) |

---

## Rollout

Operate the existing live intents (`CHECKIN_INFO`, `CHECKIN_LINK`, `WELCOME`) safely across tenants/properties.

### Do

1. Widen allowlists gradually (`MESSAGE_ORCHESTRATION_TENANTS` / `MESSAGE_ORCHESTRATION_PROPERTIES`)
2. Watch health: `GET /api/v1/reception/system/status/` → `messaging` (flags, definitions, providers, outbox depth)
3. Watch metrics / logs: `messaging_metric`, `message_orchestration_done`, `messaging_all_providers_failed`
4. After stable live on a scope: keep legacy suppressed (`suppress_legacy_automated_outbound` when live + allowlisted)
5. Retire ops reliance on legacy reminder/welcome for that scope only

### Don’t

- Open all tenants at once
- Skip Shadow on a new stack/environment
- Hard-delete `MessageDispatch` rows
- “Fix” issues by adding a new Celery sender beside the engine

### Procedure

Step-by-step Shadow → LIVE → suppression → expand allowlist:  
[messaging-orchestration-rollout-checklist.md](messaging-orchestration-rollout-checklist.md)

### Midnight WhatsApp / quiet hours

WhatsApp must not send **21:00–08:00** property-local. Policy lives in the engine (`DispatchPolicy` / ADR 0010 §11), not in legacy Celery tasks.

Safe interim + cutover order:

1. **Legacy D0 OFF** — `GUEST_CHECKIN_REMINDER_DAYS_BEFORE=7` (recreate django + celery-worker + celery-beat)
2. Engine Shadow → **LIVE** for Uzorita
3. Legacy **suppress** (`suppress_legacy_automated_outbound`)
4. Observe several days
5. Legacy retirement

### Rollback

```env
MESSAGE_ORCHESTRATION_SHADOW=true
# or
MESSAGE_ORCHESTRATION_ENABLED=false
```

Then: `docker compose up -d django celery-worker celery-beat` (recreate, not `restart`).

---

## Adoption

Migrate **new or existing** communication flows onto the frozen engine — same orchestration, no foundation rewrite.

### Candidate flows

| Flow | Notes |
|------|--------|
| Manual compose | Reception send via Dispatcher (`MANUAL` / staff trigger) |
| AI reply | Definition + trigger; providers unchanged |
| Portal notifications | Definition + channel policy |
| Invoice | Email (etc.) via registered provider |
| Review request / lifecycle | Reserved triggers as domain events appear |

### Adoption checklist (per flow)

1. Add `MessageDefinition` (+ template version, skip rules, channel policy) — no `if tenant == …`
2. Emit a Trigger (`TIME` / `MANUAL` / reserved domain kind) that materializes `MessageDispatch`
3. Reuse ProviderRegistry adapters; add a provider only if a new channel appears
4. Gate with allowlists / feature flags the same way as Rollout
5. Suppress or delete the old specialized sender **only after** live cutover is proven
6. Tests: idempotency, skip, fallback, snapshot/checksum; do not weaken ADR 0010 ban on direct senders

### Forbidden in Adoption PRs

- New parallel sender services that call SMTP / WhatsApp / Booking outside adapters
- Reshaping outbox status machine, Dispatcher ownership, or snapshot contracts without amending ADR 0010
- Event Bus / Kafka / plan DSL / BPMN as “needed for this one flow”

### Cleanup (deferred)

- **WELCOME schedule SoT:** resolve is `whatsapp_welcome_*` → platform only (legacy `welcome_*` unread / admin-hidden). **Korak 2** (later): optional copy of non-null `welcome_*` into empty `whatsapp_welcome_*`, then drop `welcome_*` columns from Property + TenantReceptionSettings. Not an engine redesign — Adoption/cleanup only. See [ADR 0010 §5](../architecture/adr/0010-messaging-orchestration-engine.md#5-schedule-settings-property--tenant--platform).

---

## Decision guide

```text
Need to turn on Uzorita / another property for CHECKIN_* / WELCOME?
  → Rollout (allowlist + checklist)

Need guests to get invoices / portal / AI / compose via the engine?
  → Adoption (new definition + trigger; same Dispatcher)

Need a new scheduling concept, outbox shape, or second orchestration layer?
  → Stop — amend ADR 0010 first (not a casual PR)
```

---

## References

- ADR [0010](../architecture/adr/0010-messaging-orchestration-engine.md) — normative engine + ban on direct senders  
- ADR [0004](../architecture/adr/0004-guest-checkin-session.md) — session SoT; engine distributes  
- Flags: `apps.communications.messaging.flags`  
- Package: `backend/apps/communications/messaging/`
