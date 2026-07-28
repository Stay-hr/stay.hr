# ADR 0013 — Obrazac PDV export (PR1 structure)

## Status

Accepted (2026-07)

## Context

After PDV-S (ADR 0012) is accepted by ePorezna, hosts still need **Obrazac PDV** (v11-0 for periods from 2026-01) so reverse-charge tax is booked on the PKK. ChatGPT-style “v3-0” / invented tag names are not SoT — the SoT is an official ePorezna export.

## Decision

1. **Independent module** `eporezna/pdv/` alongside `eporezna/pdvs/`. Shared fiscal settings and shared helpers; no tax logic shared into PDV-S mapping.
2. **Shared infra (contract):**
   - `FiscalPeriod.from_year_month` — sole YYYY-MM parser
   - `EporeznaMetadataBuilder` — Metapodaci
   - `fiscal_eporezna_readiness` — Zaglavlje readiness for PDV and PDV-S
   - XML helpers + `build_filename`
3. **Metapodaci invariant:** Metadata must be byte-identical across all ePorezna exports except for Naslov, Uskladjenost, timestamp and UUID.
4. **Empty export gate** is **source fiscal data for period** (today: `ForeignServiceInvoice`; tomorrow: other ledgers). Builders must not hard-code “invoice-only” wording as the domain concept.
5. **`PDVBuilder` intentionally contains no tax calculation logic.** PR1 serializes the official v11-0 structure only (zero Tijelo matching the empty ePorezna reference). Tax computation will be introduced in a dedicated mapping layer once an official filled reference export is available (PR1.1).
6. Clock / Uuid injection — only Metapodaci Datum / Identifikator vary.

## Consequences

- PR1.1 fills `Podatak210` / `310` / `400` (etc.) only from a filled ePorezna SoT — no invented II.10/III.10 rules.
- Later: PDV totals should align with PDV-S `I2` for the same period.
- HUB-3A payment slips are out of scope for this ADR.
