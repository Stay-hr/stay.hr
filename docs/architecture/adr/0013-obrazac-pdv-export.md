# ADR 0013 — Obrazac PDV export

## Status

Accepted (2026-07); amounts filled in PR1.1

## Context

After PDV-S (ADR 0012) is accepted by ePorezna, hosts still need **Obrazac PDV** (v11-0 for periods from 2026-01) so reverse-charge tax is booked on the PKK. ChatGPT-style “v3-0” / invented tag names are not SoT — the SoT is an official ePorezna export / UI field numbering.

## Decision

1. **Independent module** `eporezna/pdv/` alongside `eporezna/pdvs/`. Shared fiscal settings and shared helpers.
2. **Shared infra (contract):** `FiscalPeriod.from_year_month`, `EporeznaMetadataBuilder`, `fiscal_eporezna_readiness`, XML helpers, `build_filename`.
3. **Metapodaci invariant:** byte-identical across ePorezna exports except Naslov, Uskladjenost, timestamp and UUID.
4. **Empty export gate** = **source fiscal data for period** (today: `ForeignServiceInvoice`).
5. **`PDVAmountMapper` is the only place that maps invoices → form amounts.** `PDVBuilder` serializes only.
6. **Field mapping (paušalist / no pretporez deduction), confirmed against ePorezna UI:**
   - **II.10** `Podatak210` — Primljene usluge iz EU 25%: `Vrijednost` = Σ `taxable_amount`, `Porez` = base × 0.25
   - **II UKUPNO** `Podatak200` — same totals when only II.10 is non-zero
   - **III.10** `Podatak310` — pretporez stays **0** (no deduction)
   - **IV** `Podatak400` — obveza za uplatu = II porez (− III = 0)
7. Clock / Uuid injection — only Metapodaci Datum / Identifikator vary.

## Consequences

- PDV II.10 base aligns with PDV-S `I2` for the same period (clears ePorezna yellow cross-check).
- HUB-3A payment slips remain out of scope.
