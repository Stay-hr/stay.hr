# Channex Promotions API — Phase 0 probe

Probe za [ADR 0009 — Channel Promotions](../architecture/adr/0009-channel-promotions.md).

**Phase 0 status: CLOSED** (2026-07-24).

**Ishod:** Channex support — **nema Promotions API** za PMS. Phase B = **`BookingPromotionProvider`**. ADR → **Accepted**.

**Do Phase A:** nema Django modela dok se ne krene namjerno na Korak 2. Do tada ručno: Booking.com Extranet (± Channex UI).

---

## stay.hr scope

| | |
|--|--|
| Admin property | [property/4](https://admin.stay.hr/admin/properties/property/4/change/) — `uzorita` / Uzorita B&B |
| IntegrationConfig | id=2, `property_id=4`, tenant `uzorita` |
| Channex property UUID | `bca8473d-7c36-4986-bcdb-b5760b633283` |
| Booking.com channel UUID | `b15001d9-cc2c-47b0-81d5-b459d8f6ebf1` |
| Booking hotel ID | `4181954` |

---

## Rezultat

| Polje | Vrijednost |
|-------|------------|
| Phase 0 | **CLOSED** |
| Datum live probe | 2026-07-24 |
| Datum odgovora supporta | 2026-07-24 |
| Ticket / thread | Channex support — **no Promotions API** |
| CREATE | **no** (support + `POST …/promotions` → 404) |
| UPDATE | **no** |
| DELETE | **no** |
| LIST | **no** (`GET …/promotions` → 403; support: no API) |
| Booking promotion ID | n/a via Channex |
| Idempotency / external ref | n/a via Channex |
| Webhook | **no** |
| Polling | **no** |
| Basic Deal fields | n/a via Channex |
| Access / entitlement | n/a — no API to entitle |
| Phase B provider | **`BookingPromotionProvider`** |
| ADR log updated | **yes** |
| ADR Status | **Accepted** |

---

## Live API probe (stay.hr → app.channex.io)

**Datum:** 2026-07-24 · Credentials: IntegrationConfig id=2 · property 4  
**Sanity:** `GET /room_types` → **200**

| Request | Status | Napomena |
|---------|--------|----------|
| `GET /promotions` | **404** | |
| `GET /channels/{channel_id}/promotions` | **403** | support: nema API |
| `POST` / `PUT` / `PATCH` / `DELETE` isto | **404** | |

Javni docs ([docs.channex.io](https://docs.channex.io/)): nema Promotions collection; Channel API = whitelabel / ask us; webhooks bez `promotion_*`.

---

## Decision matrix (primijenjeno)

| Ishod | Phase B adapter | ADR Status |
|-------|-----------------|------------|
| Nema API / samo UI | **`BookingPromotionProvider`** | **Accepted** |

Sljedeće: **Korak 2** — Django modeli (`ChannelPromotion`, `ChannelPromotionLink`, `PromotionOutbox`) kad se krene implementacija. Do tada Basic Deal ručno u Booking Extranetu.
