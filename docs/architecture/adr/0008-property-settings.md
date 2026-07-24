# ADR 0008: Property Settings (source of truth, PortalRenderer, settings API)

## Status

**Accepted** (2026-07) — normative contract; implementation follows PR-D0…PR-F.

| PR | Scope | Ships |
|----|-------|-------|
| **PR-msg** | Two-message portal CTA on BOOKING/WhatsApp | ✅ (compose + distribute; see [ADR 0007](0007-guest-portal.md)) |
| **PR-D0** | Settings shell: capabilities root, properties list, nav, reserved stubs | ✅ code |
| **PR-D1** | Guest settings GET/PATCH, preview, schema_version, validation, ETag | ✅ code |
| **PR-D2** | Share API (`kind` + `target`) + UI | ✅ code |
| **PR-E** | General + check-in section endpoints + UI | ✅ code |
| **PR-F** | Automation section endpoints + UI | ✅ code |

**Reserved in this ADR only (no storage/UI yet):** draft vs published columns, CDN/`asset_id` upload, preview response cache, fine-grained token scopes beyond `reception:read` / `reception:write`.

Supersedes the original “PR-D guest-portal editor” framing in [ADR 0007](0007-guest-portal.md): the guest portal is a **view**, not a separate edit module.

---

## Summary

**Property is the source of truth** for guest-facing content (`guest_info`, contact, `self_service_*`) and related property settings. Reception UI on `app.stay.hr/settings` reads and writes through a sectioned settings API. Public portal, reception preview, and later channel snippets all render through one **`PortalRenderer`**. Share and compose sit beside settings, not inside the portal page.

---

## Context / Problem statement

ADR 0007 shipped a stable public portal URL and frozen `GuestPortalContext`. Staff still lacked a structured write path (Django admin / seed only). An early sketch proposed a dedicated `…/guest-portal/` reception editor. That couples “edit content” to “portal product” and risks divergent builders for preview vs public.

Requirements:

- One write path per property section; portal is read-only projection
- Frontend tabs/capabilities from the API (no hardcoded feature flags in UI)
- Optimistic concurrency on settings writes
- Share portal (and later kinds) via a small contract, not ad-hoc compose in views
- Room for draft/publish, media CDN, and finer permissions without path churn

---

## Decision

### Property as source of truth

```text
Reception UI  →  GET/PATCH …/settings/*  →  PropertySettingsService
                                           →  GuestSettingsService (guest section)
                                           →  Property.guest_info + contact + self_service_*
                                           →  PortalRenderer
                                                → public /g/{token}
                                                → GET …/guest/preview
                                                → (later) WA / email snippets
```

- **Do not** introduce a separate `…/guest-portal/` write API.
- Views stay thin; merge/validation live in services / `guest_info` helpers.

### Unique `PortalRenderer`

There is **one** builder for portal `sections` + localized `content`. Preview and public portal call the same renderer (property-scoped preview and reservation-scoped public). Today’s `build_guest_portal_context` evolves into a stable `PortalRenderer` API; do not maintain parallel `PortalBuilder` / `PreviewBuilder`.

Preview: `GET …/settings/guest/preview?lang=&on_date=` — same payload shape as public portal. Cache invalidation on PATCH is reserved; cache implementation is **not** in PR-D1.

### API namespace (per property)

```
GET  /api/v1/reception/settings
GET  /api/v1/reception/properties/
GET|PATCH /api/v1/reception/properties/{id}/settings/general
GET|PATCH /api/v1/reception/properties/{id}/settings/guest
GET       /api/v1/reception/properties/{id}/settings/guest/preview?lang=&on_date=
GET|PATCH /api/v1/reception/properties/{id}/settings/checkin
GET|PATCH /api/v1/reception/properties/{id}/settings/automation
POST      /api/v1/reception/properties/{id}/settings/share
```

**Reserved** (ADR + frontend stub 404/disabled until a dedicated PR):

```
…/settings/security
…/settings/integrations
…/settings/branding
…/settings/localization
…/settings/payments
…/settings/reviews
…/settings/users
```

Frontend mirrors paths: `/settings`, `/settings/general`, `/settings/guest`, `/settings/checkin`, `/settings/automation`, and the same reserved set. WhatsApp stays under `/whatsapp/*` until Communication/Integrations PRs.

### Capabilities (not ad-hoc flag sprawl)

`GET /api/v1/reception/settings` returns:

```json
{
  "capabilities": {
    "guest_settings": true,
    "preview": true,
    "share": true,
    "automation": true,
    "checkin": true,
    "general": true
  },
  "tabs": {
    "general": true,
    "guest": true,
    "checkin": true,
    "automation": true
  }
}
```

Frontend **does not hardcode** tabs — it reads `tabs` / `capabilities`. Global env `RECEPTION_PROPERTY_SETTINGS=false` disables the whole Settings surface (all capabilities false + nav hidden). Later per-tenant gating must not invent new boolean flags scattered in UI code.

### General + check-in sections (PR-E)

`GET|PATCH …/settings/general` — property identity: `name`, `address`, `timezone`, `language`; `slug` is read-only on GET. Uses shared `Property.settings_version` ETag / `If-Match` (same as guest).

`GET|PATCH …/settings/checkin` — arrival window: `check_in_time`, `check_out_time`, `check_in_latest_time` (nullable), `guest_checkin_opens_days_before`. After-hours policy and auto-reply flags live under **automation** (PR-F).

Both emit `PropertySettingsUpdated` with `section` = `general` | `checkin` and a `change_summary[]`.

### Automation section (PR-F)

`GET|PATCH …/settings/automation` — after-hours arrival + guest auto-replies:

- `after_hours_arrival_policy` — `contact` | `not_allowed`
- `after_hours_contact_phone` — optional; falls back to `property.contact` when empty and policy is `contact`
- `guest_arrival_auto_reply_enabled`
- `guest_parking_auto_reply_enabled`

Same `settings_version` ETag / `If-Match` as other sections. Emits `PropertySettingsUpdated` with `section` = `automation`.

### Permissions

| Scope | Meaning |
|-------|---------|
| `reception.settings.read` | GET settings / preview |
| `reception.settings.write` | PATCH settings |
| `reception.settings.share` | POST share |

**PR-D0/D1 enforcement:** map to existing `reception:read` / `reception:write` (share requires write). Fine-grained scopes are named in the API contract now; token model adds them when reception auth supports them.

### Guest settings contract

#### Schema version

Every guest GET/PATCH payload includes `schema_version` (v1 = `1`) plus structured sections: `wifi`, `parking`, `arrival`, `breakfast`, `contact`, `self_service`, `guide`.

Server rejects PATCH with unknown / future `schema_version` (**400**) until a migrator exists. Storage remains `Property.guest_info` JSON; version lives on the API/DTO layer (and optionally `guest_info.schema_version`).

#### Structured sections

Merge via helpers in `apps/properties/guest_info.py` — not raw JSON dumps in views.

#### Media abstraction

API does not expose bare `image_url` as the only contract. Fields use:

```json
"entrance": { "media": { "asset_id": null, "url": "/api/..." } }
"guide.steps[].media": { "asset_id": null, "url": "..." }
```

**v1:** `asset_id` always `null`; `url` is path/public URL from seed. CDN/upload later fills `asset_id` without breaking clients (**reserved**).

#### Localization

- PATCH/GET return `texts` maps per language.
- Property-level `enabled_languages` (default = existing template langs) is part of the contract; v1 may use a fixed set until general/localization ships. Serializers filter empty languages by `enabled_languages` when that field exists.

#### Validation

Shared module (e.g. `properties/guest_settings_validation.py`): SSID/password max length, WhatsApp/phone normalize, maps URL scheme, caption max length, max guide steps, supported language codes. Same limits documented for frontend (constants export or OpenAPI descriptions).

#### Optimistic locking

Add `Property.settings_version` (`PositiveInteger`, default `1`, +1 on every successful settings PATCH) — preferred over timestamp-only ETags.

- `GET …/settings/guest` → weak `ETag` from `settings_version`
- `PATCH` requires `If-Match`; mismatch → **409 Conflict** with current body / new ETag

#### Draft vs published (**reserved**)

API shape from day one:

```json
{ "publication": { "state": "published", "draft_available": false } }
```

Dual storage (`draft_guest_info` / published) is **not** in PR-D1. A later PR adds draft without changing paths (`?draft=true` on GET/PATCH/preview, `POST …/guest/publish`).

### Share API

`POST …/settings/share`:

```json
{
  "kind": "portal",
  "target": "reservation",
  "reservation_id": 82,
  "channel": "booking"
}
```

| Field | v1 | Later |
|-------|----|-------|
| `kind` | `portal` | `guide` \| `invoice` \| `payment` \| `review` |
| `target` | `reservation` | `guest` \| `thread` |

Default `channel` from completed check-in `created_from` when omitted. Portal send reuses the PR-msg two-message compose (CTA+sign-off, then URL) on BOOKING/WhatsApp.

### Service layer

| Service | Responsibility |
|---------|----------------|
| `PropertySettingsService` | capabilities, property list, section routing |
| `GuestSettingsService` | get/patch guest DTO, schema_version, validation, version bump |
| `GeneralSettingsService` | get/patch identity (name/address/timezone/language) |
| `CheckinSettingsService` | get/patch check-in/out times + opens-days |
| `AutomationSettingsService` | get/patch after-hours policy + auto-reply flags |
| `PortalRenderer` | sole builder of sections+content (public + preview) |
| `ShareService` | kind/target dispatch, portal ensure+send |
| `PreviewService` | optional thin wrapper (lang / `on_date`) around PortalRenderer |

### Domain events, analytics, audit

On successful write/share emit (Django signal or existing event-bus pattern):

- `PropertySettingsUpdated` / `GuestSettingsUpdated` — `section`, `property_id`, `actor_id`, `settings_version`, `change_summary[]`
- `GuestPortalShared` — `reservation_id`, `channel`, `kind`

Analytics names (telemetry later, not a gate): `portal_preview_opened`, `portal_shared`, `guest_settings_saved`, `guest_settings_published` (when draft/publish exists).

**Audit (v1 with events):** `updated_by` (api_application / user id when available) + `change_summary` string list (`wifi.password`, `parking`, …). Activity Timeline UI later consumes the same events. Persistent audit tables beyond events are **reserved**.

### Placeholder engine

One helper shared by renderer + compose:

`{guest_name}` `{property_name}` `{room_name}` `{room_code}` `{key_label}` `{checkin_date}` `{checkout_date}` `{wifi_ssid}` `{wifi_password}`

Preview without a reservation uses safe sample placeholders. Same engine later for email/WA/PDF/kiosk.

### Frontend IA

- Top nav **Postavke** when settings root capabilities say Settings is on
- SubNav from `tabs`
- Property picker from `GET /reception/properties/`
- `/settings` → first enabled tab (usually guest)
- Guest page: editor + preview panel (preview = API only)

---

## Consequences

- Portal remains the public read model from ADR 0007; settings own writes.
- Adding a settings section = new path under `/settings/{section}` + capability/tab bit — not a new top-level product module.
- Draft/CDN/fine scopes can land without renaming routes or inventing a second portal editor.
- Ops editing path moves to `app.stay.hr/settings` (see [guest-portal.md](../../operations/guest-portal.md)).

---

## References

- Plan: Property Settings architecture (replaces original PR-D editor)
- Guest portal: [0007-guest-portal.md](0007-guest-portal.md)
- Ops: [guest-portal.md](../../operations/guest-portal.md)
- Evolve: `backend/apps/reservations/guest_portal_context.py` → `PortalRenderer`
- Services: `backend/apps/properties/` (`guest_settings_service.py`, `general_settings_service.py`, `checkin_settings_service.py`, `automation_settings_service.py`, `portal_renderer.py`, `share_service.py`, …)
- API: reception property settings views + `reception_urls.py`
- UI: `web/reception/app/settings/**`, `ReceptionNav.tsx`
