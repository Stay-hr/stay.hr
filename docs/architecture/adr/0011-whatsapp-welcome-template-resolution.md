# ADR 0011: WhatsApp welcome template resolution

## Status

**Accepted** (2026-07-25)

Implemented across Phases 1–5 of the welcome-template map sync; this ADR is the normative contract.

| Phase | Scope | Ships |
|-------|-------|-------|
| **1** | Immutable `WELCOME_TEMPLATE_REGISTRY` + `resolve_welcome_template` (additive) | ✅ |
| **2** | Migrate all send callers; remove split helpers | ✅ |
| **3** | Merge-only seeds, startup/admin validation, `verify_whatsapp_templates` | ✅ |
| **4** | `welcome_template_resolved` log + `system/status` health block | ✅ |
| **5** | Matrix / override / regional / snapshot / architecture + E2E PL payload | ✅ |
| **6** | This ADR + module algorithm comment + ops verify (#1020) | ✅ |

---

## Summary

**Why:** Incomplete `whatsapp_templates.welcome` maps caused Meta `#132001` when guests spoke an approved language missing from config — e.g. reservation `#1020` (PL): code picked `stay_welcome_en` while Meta language stayed `pl`.

**How:** One immutable registry is the sole source of truth for `(template_name, meta_language)`. Every welcome send goes through `resolve_welcome_template(...)` → `ResolvedWelcomeTemplate` (never `None`). Template name and Meta language are inseparable.

**Normative rule (required):**

> **Welcome outbound must use `resolve_welcome_template` only.** Choosing a template name and a Meta language independently is forbidden. Seeds are merge-only. The registry is the only code SoT for Meta-approved welcome languages.

Package: `backend/apps/integrations/whatsapp/welcome_template.py` (+ `welcome_template_config.py` for merge/validate/health).

---

## Context / Problem statement

Auto check-in welcome (legacy path and Messaging Engine WhatsApp adapter) needs a Meta-approved `stay_welcome_*` template plus a matching Graph API language code.

The old helpers:

```python
welcome_template_name(config, lang)   # map.get(lang) or map.get("en")
welcome_meta_language_code(lang)      # independent of chosen name
```

diverged whenever config omitted a language that Meta already had APPROVED (e.g. `pl`). That class of mismatch is not fixable by one-off config edits alone.

Requirements:

- Single SoT for approved langs and their Meta language codes (`ua` → Graph `uk`)
- Deterministic resolve with audited `source` + `match` on every result
- Incomplete config maps still resolve via registry DEFAULT (not English+wrong lang)
- Callers never invent their own English fallback
- Seeds never wipe custom overrides
- Observable via structured logs and `system/status`

---

## Decision

### 1. Single immutable registry (SoT)

```python
@dataclass(frozen=True)
class TemplateDefinition:
    template_name: str   # stay_welcome_pl
    meta_language: str   # Graph API language (ua → uk)

WELCOME_TEMPLATE_REGISTRY: Mapping[str, TemplateDefinition]  # MappingProxyType
```

- Derived only from the registry: `META_APPROVED_LANGUAGES`, seed defaults, validation allowlist, test matrix.
- Import-time validation raises `ImproperlyConfigured` on empty registry, missing `en`/`hr`, duplicate names, non-`stay_welcome_*` names, non-lowercase keys, or `ua.meta_language != "uk"`.
- Live Meta-approved keys: `cs, de, en, es, fr, hr, hu, it, lt, nl, pl, ro, sk, ua`.

### 2. One public resolve API — ban split name/lang

```python
resolved = resolve_welcome_template(
    language=lang,
    property_config=...,
    platform_config=...,
)
send_template_message(
    template_name=resolved.template_name,
    language_code=resolved.meta_language,
    ...
)
```

**Forbidden:** `welcome_template_name` / `welcome_meta_language_code` (removed) and any new helper that returns only a name or only a language for welcome sends.

`ResolvedWelcomeTemplate` always includes:

| Field | Meaning |
|-------|---------|
| `template_name` | Meta template name |
| `meta_language` | Graph API language code |
| `requested_language` | Raw caller input |
| `resolved_language` | Registry/config key that matched |
| `source` | `ResolutionSource`: property / platform / default / english |
| `match` | `ResolutionMatch`: exact / base |

Never `None`. English fallback: `source=ENGLISH`, `match=EXACT`, `resolved_language=en`, `meta_language=en`.

### 3. Resolution algorithm (fixed order)

```text
normalize(requested)
  → try exact key, then base language (e.g. en-US → en)
      → property config welcome map
      → platform config welcome map
      → WELCOME_TEMPLATE_REGISTRY DEFAULT
  → english fallback (stay_welcome_en + meta en)
```

`normalize_language`: trim/lower; `_` → `-`; ISO `uk` → internal `ua`; empty/None → `en`; regional tags keep region so exact-then-base works.

Canonical comment lives at the top of `welcome_template.py`.

### 4. Merge-only configuration

Seeds (`seed_platform_whatsapp_config`, `seed_uzorita_whatsapp_config`) and `merge_whatsapp_welcome_templates` fill **missing** registry langs only. Non-empty custom values are never overwritten. Unrelated config keys (phone, header image, tokens) are preserved.

Startup `validate_welcome_templates()` and IntegrationConfig admin enforce map integrity (hard errors vs soft warnings). Ops: `manage.py verify_whatsapp_templates` (`--live-meta` optional).

### 5. Observability

- Structured log `welcome_template_resolved` with `source=` + `match=` (WARNING when `source=english`).
- Health block: `GET …/system/status/` → `messaging.welcome_templates`.

---

## Consequences

### Positive

- Template name and Meta language cannot diverge on the send path.
- Incomplete config no longer maps `pl` → `stay_welcome_en` + `language=pl` (#1020 class).
- Adding a Meta-approved language is one registry entry + merge seed; tests/matrix derive from SoT.
- Logs and health make misconfiguration and English fallback visible without reconstructing the algorithm.

### Negative

- Every welcome send site must take both fields from `ResolvedWelcomeTemplate` (architecture tests enforce this).
- Custom welcome map values still need correct Meta language pairing via registry key (or English fallback).

### Forbidden without a new ADR

- Parallel approved-language lists or a second welcome name map hand-maintained beside the registry
- Reintroducing public split helpers for welcome sends
- Overwrite-style seeds that wipe custom overrides
- Caller-side English (or other) fallback after resolve

---

## Alternatives considered

| Alternative | Why not chosen |
|-------------|----------------|
| One-off config edit (`pl: stay_welcome_pl` only) | Fixes one guest; same bug returns for next missing lang |
| Fallback name to `en` but keep guest language | Causes Meta `#132001` (the #1020 failure mode) |
| Keep `welcome_template_name` + `welcome_meta_language_code` with soft deprecation | Callers reintroduce mismatch within months |
| Parallel `META_APPROVED_LANGUAGES` list + registry | Two SoTs drift over time |
| Prometheus counters in v1 | Repo has no Prometheus stack; structured logs suffice |

---

## References

- Implementation: `backend/apps/integrations/whatsapp/welcome_template.py`, `welcome_template_config.py`
- Call sites: `whatsapp_autocheckin_tasks`, `guest_message_send`, `guest_message_whatsapp_v2`, `operator_arrival_confirm`
- Ops: `manage.py verify_whatsapp_templates`, `manage.py merge_whatsapp_welcome_templates`
- Related: [ADR 0010 — Messaging Orchestration Engine](0010-messaging-orchestration-engine.md) (WELCOME intent still uses this resolver via the WhatsApp adapter)
- Incident class: reservation `#1020` (2026-07-25) — PL guest, incomplete welcome map → Meta `#132001`
