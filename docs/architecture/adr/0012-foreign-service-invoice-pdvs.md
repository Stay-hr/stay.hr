# ADR 0012 — Foreign service invoices & PDV-S export (PR1)

## Status

Accepted (2026-07)

## Context

Croatian hosts who receive Booking.com (and later Airbnb / Expedia) commission invoices from other EU member states must file **Obrazac PDV-S** (reverse charge on received services). stay.hr already has tenant-scoped fiscal settings for guest invoices; we need a generic inbound path that is not Booking-specific.

## Decision

1. **`ForeignServiceInvoice`** is the only persisted inbound model. Columns stay provider-agnostic. Provider extras live only in immutable **`parsed_payload`**.
2. Pipeline: **PDF bytes → parser (`can_parse` / `parse`) → DTO → validator → ImportService → model → `PDVSBuilder` → XML**. Never PDF → XML.
3. **`document_sha256`** is SHA-256 of **original PDF bytes** (not extracted text). Re-import of the same bytes is **idempotent** (return existing row).
4. Parser registry is an **ordered list**; first `can_parse() == True` wins (allows BookingPdfParserV1 / V2).
5. **`PDVSBuilder.build(tenant, period)`** loads invoices itself. Clock / Uuid are injected so Metapodaci Datum / Identifikator are the only non-deterministic fields.
6. **PR1 exported Zaglavlje + empty `Isporuke`.** PR1.2 fills `Isporuke` via `PDVSLineMapper` using ePorezna reference exports.

## Consequences

- Adding Airbnb/Expedia = new parser class + registry registration only.
- Improving a parser does not rewrite historical `parsed_payload`; re-import or a new document is required.
- XSD in-repo is a structural stand-in aligned with ePorezna export; official schema may replace it later without changing the builder API.

## Reception surface (PR1.1)

- Thin Reception API wraps `ImportService` / `PDVSBuilder` / `fiscal_pdvs_readiness` (SoT in `billing/services/eporezna/readiness.py`).
- UI lives as an independent **PDV-S (EU usluge)** section on the property-financial page host; it does **not** use checkout report filters.
- **PDV-S `period`** = `ForeignServiceInvoice.tax_period` only — never merge with property-financial checkout `from`/`to`.

## Isporuke mapping (PR1.2)

- **`PDVSLineMapper` is the only place that implements mapping from `ForeignServiceInvoice` to PDV-S form lines.**
- Aggregate by normalized `(country_code, vat_id)`; amounts are `Decimal` quantized to 2 places.
- Form fields: `I1` = goods, `I2` = services. Current foreign-service invoices map taxable amount → `I2` (goods `I1=0`).
- `PDVSBuilder` serializes `PDVSLine` only and rejects empty tax periods.
