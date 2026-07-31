# Channex — Uzorita × Booking.com (produkcija)

Booking.com extranet veza **Channex.io** za objekt **Luxury Room Uzorita B&B**.

## Identifikatori

| Polje | Vrijednost |
|-------|------------|
| Booking.com hotel ID | `4181954` |
| Channex property | Luxury Room Uzorita B&B |
| Channex property UUID | `bca8473d-7c36-4986-bcdb-b5760b633283` |
| Channex account | [app.channex.io](https://app.channex.io/) (Fine Star d.o.o.) |
| stay.hr tenant | `uzorita` (property slug `uzorita`) |
| Valuta | EUR |

## Channel — General Settings (Channex UI)

| Polje | Vrijednost |
|-------|------------|
| Title | Booking.com - Uzorita |
| Hotel ID | `4181954` |
| Currency | EUR |
| Pricing Type | **OBP** (occupancy-based pricing) |
| Booking notification email | `room_reservations@uzorita.hr` ✅ |
| Test Connection | Successful |

---

## Mapping — Booking.com → Channex → stay.hr

1 fizička soba = 1 stay.hr `Unit.code` = 1 Channex room type.

### Tablica mapiranja (korak Mapping u Channex channel wizardu)

| Booking.com listing | Booking room ID | Rate plan | stay.hr | Channex room type |
|---------------------|-----------------|-----------|---------|-------------------|
| Deluxe King Room | `418195401` | Standard Rate `12846654` | **R1** | Luxury Room Uzorita - **R1** |
| Deluxe King Room | `418195404` | Standard Rate `12846654` | **R2** | Luxury Room Uzorita - **R2** |
| Deluxe Triple Room | `418195403` | Standard Rate `12846654` | **R3** | Luxury Room Uzorita - **R3** |
| Deluxe Double Room | `418195405` | Standard Rate `12846654` | **R6** | Luxury Room Uzorita - **R6** |
| Standard King Room | `418195406` | Standard Rate `12846654` | **R4** | Standard King Room **R4** |

U Channex Mapping tabu za **svaki** Booking red odaberi odgovarajući **Channel Manager** room type (desna strana). Svi koriste isti rate plan ID `12846654`.

### Kako smo potvrdili mapiranje

| Dokaz | R1 | R2 | R3 | R6 | R4 |
|-------|----|----|----|-----|-----|
| Booking PDF / XLS nazivi | `… - R1` / generic King→R1 | `… - R2` | Deluxe Triple | Deluxe Double | Standard King Room |
| Channex room type ID (Booking) | 418195401 | 418195404 | 418195403 | 418195405 | 418195406 |
| Rezervacije u bazi (`ReservationUnit`) | Uzorita R1, Deluxe King | Uzorita R2, `… - R2` | Deluxe Triple | Uzorita R6, Deluxe Double | Standard King Room R4 |

**Pažnja — dva Deluxe King Room:** Booking.com ima **dva** odvojena listinga (401 i 404). Generic `Deluxe King Room` bez sufiksa stay.hr povijesno mapira na **R1** (`unit_mapping.py`); u Channexu ih **ne smiješ** oba mapirati na isti room type. **Standard King Room** (`418195406`) mapira na **R4**.

### Occupancy (Booking ikona / Channex / stay.hr)

| Soba | Booking listing | stay.hr kapacitet |
|------|-----------------|-------------------|
| R1 | King, max 2 | 2 / 2 / 1 / 1 |
| R2 | King, max 3 | 3 / 2 / 2 / 1 |
| R3 | Triple, max 4 gostiju (**max 3 odrasla**) | 4 / 3 / 3 / 3 |
| R6 | Double, max 4 gostiju (**max 3 odrasla**) | 4 / 3 / 3 / 3 |
| R4 | Standard King, max 2 | 2 / 2 / 1 / 1 |

OBP pricing na kanalu — cijene po occupancy moraju biti usklađene u Channex rate planu i stay.hr syncu.

### Vizualno (Channex Mapping)

```
Booking.com                          Channex (Channel Manager)
─────────────────────────────────    ─────────────────────────────
Deluxe Double Room (418195405)   →   Luxury Room Uzorita - R6
  Standard Rate (12846654)

Deluxe King Room (418195401)     →   Luxury Room Uzorita - R1
  Standard Rate (12846654)

Deluxe King Room (418195404)     →   Luxury Room Uzorita - R2
  Standard Rate (12846654)

Deluxe Triple Room (418195403)   →   Luxury Room Uzorita - R3
  Standard Rate (12846654)

Standard King Room (418195406)   →   Standard King Room R4
  Standard Rate (12846654)
```

---

| Polje | Vrijednost |
|-------|------------|
| Provider | Channex.io |
| Status | **Active** |
| Aktivirano | 2026-05-26 07:38:20 |
| Partner tier (Booking) | Premier Plus |

Prije spajanja svi tipovi pristupa imali su **Current provider: None** — prvi channel manager na Booking.com extranetu za te kategorije.

### Odobrena prava i što Channex može uređivati

Kanonski popis (prava + ✅/❌ funkcije): [channex-booking-editable-settings.md](channex-booking-editable-settings.md).

---

## Gdje što uređivati (operativni vodič)

| Što | Gdje | Napomena |
|-----|------|----------|
| **Cijene i dostupnost (live sync)** | stay.hr recepcija → Channel | Kanonski izvor: **stay.hr** `RatePlanDay` → Channex → Booking.com |
| **Room types / mapping R1–R6** | Channex UI (app.channex.io) | Count=1 po fizičkoj sobi; mapiranje na Booking listing u channel Mapping |
| **Occupancy (max guests, adults, …)** | Channex room type **i** stay.hr `Unit` | Moraju biti usklađeni — vidi [channex-uzorita-mapping.md](channex-uzorita-mapping.md) |
| **Rate planovi** | Channex UI | UUID-ovi idu u stay.hr `IntegrationConfig` |
| **Rezervacije inbound** | Channex webhook → stay.hr | Tek kad je production Channex config + webhook aktivan |
| **Poruke gostima (Booking.com)** | Channex webhook `message` + stay.hr API | Inbound u `ChannexMessage`; odgovor: `POST /api/v1/reception/reservations/{id}/channex-messages/` |
| **Sadržaj, fotke, recenzije** | Channex UI (ili Booking extranet) | stay.hr **recenzije** (inbox + odgovor) — vidi [guest-reviews-channex.md](../operations/guest-reviews-channex.md) |
| **Promocije Booking.com** | Booking Extranet (± Channex UI) danas; budući stay.hr → **`BookingPromotionProvider`** | [ADR 0009](../architecture/adr/0009-channel-promotions.md) — Phase 0: Channex nema Promotions API |
| **Ručne rezervacije / timeline** | stay.hr recepcija | Outbound u Channex tek kad `channel_manager=channex` |

---

## stay.hr — trenutni status integracije (2026-05-26)

| Stavka | Status |
|--------|--------|
| Booking.com → Channex provider | ✅ Active |
| Booking.com channel | ✅ Active (`Booking.com - Uzorita`) |
| Property webhook | ✅ test 200 |
| stay.hr `IntegrationConfig` production | ✅ seeded (id=2, active) |
| stay.hr `channel_manager` | ✅ **channex** |
| Full sync (500 days) | ✅ 2026-05-26 — availability task ok |
| Guest messaging backend (webhook + API) | ✅ stay.hr `ChannexMessage` + reception API |
| Smoobu disconnect (Booking.com extranet) | ✅ (prije Channex connecta) |

---

## Channex UI — tipični zadaci

| Zadatak | Channex put |
|---------|-------------|
| Channel Mapping (Booking listing → room type) | Property → Channels → Booking.com → Mapping |
| Room types R1–R6 | Property → Room Types |
| Rate plans | Property → Rate Plans |
| API key | Settings → API Keys |
| Webhook prema stay.hr | Settings → Webhooks |
| Messaging & Reviews app | Property → Apps → uključiti prije guest poruka |
| Pregled rezervacija s kanala | Bookings |

Webhook URL (stay.hr):

```text
https://api.stay.hr/api/v1/integrations/channex/webhook/?provider=stay&env=staging
```

Secret: `CHANNEX_WEBHOOK_SECRET` u `.env` / IntegrationConfig.

### Guest messaging (stay.hr backend)

1. U Channexu uključiti **Messaging & Reviews** app za Uzorita property.
2. Na postojećem webhooku dodati event **`message`** s **`send_data=true`** (uz postojeće `booking*` evente).
3. Inbound poruke spremaju se u **`ChannexMessage`** (admin → Integrations → Channex messages).
4. API (recepcija, bez UI u fazi 1):

| Metoda | Ruta |
|--------|------|
| GET | `/api/v1/reception/reservations/{id}/channex-messages/` |
| POST | `/api/v1/reception/reservations/{id}/channex-messages/` — body `{"message": "..."}` |

GET prazne liste automatski povlači poruke iz Channexa (`GET /bookings/{id}/messages`). Query `?sync=0` isključuje pull.

### Guest reviews (stay.hr backend)

1. Na webhooku dodati evente **`review`** i **`updated_review`** s **`send_data=true`**.
2. Inbound recenzije u **`ChannexReview`** (admin → Integrations → Channex reviews).
3. API (recepcija + web + Flutter):

| Metoda | Ruta |
|--------|------|
| GET | `/api/v1/reception/reviews/` |
| GET | `/api/v1/reception/reviews/{id}/` |
| POST | `/api/v1/reception/reviews/{id}/reply/` |
| POST | `/api/v1/reception/reviews/{id}/guest-review/` |
| GET | `/api/v1/reception/reservations/{id}/reviews/` |

Backfill: `python manage.py sync_channex_reviews --tenant-slug=uzorita`

Runbook: [docs/operations/guest-reviews-channex.md](../operations/guest-reviews-channex.md).

Deploy nakon code changea:

```bash
docker compose exec django python manage.py migrate
docker compose build django && docker compose up -d django celery-worker
```

---

## Pricing policy 2026 (sezona srpanj–kolovoz)

**Primijenjeno:** 2026-05-28 — Booking **reduction model**: puna cijena na max odraslih, manje gostiju −5 €.

**Ispravka Booking sync (2026-05-28, reduction OBP R3/R6):**

| Problem | Uzrok | Fix |
|---------|-------|-----|
| Booking extranet prikazuje povećanje od 1 odr. umjesto smanjenja od pune cijene | stay.hr + Channex koristili increment model (primary occ=1, push baze 147 €) | Reduction model: primary **max odrasli**, push **normal** cijene, channel **occupancy = max** |
| R3 Booking extranet **147 / 157 / 167** (ranije) | Channel mapping RLO + dupli occ=3 red | OBP + ispravan sync (vidi fazu 1 ispod) |

**Ranija ispravka (2026-05-28, faza increment → zatim zamijenjena reduction modelom):**

| Problem | Fix |
|---------|-----|
| R3 **147 / 157 / 167** umjesto **147 / 152 / 157** | Channel R3 → OBP (outbox #110) |
| R6 dupli mapping | Jedan OBP mapping (outbox #111) |

**Reduction OBP (2026-05-28, outbox #113):**

| Akcija | Detalj |
|--------|--------|
| Rate plan R3/R6 | Primary occupancy **3**, `rate_mode: auto`, `±5 €` |
| Rate plan R1/R2 | Primary occupancy **2** |
| Channel mapping | R3 `418195403` **OBP occ=3** · R6 `418195405` **OBP occ=3** · R1/R2 **OBP occ=2** · R4 `418195406` **OBP occ=2** |
| Re-push srpanj–kolovoz 2026 | R3/R6 **157 €** (baza 147 €) · R1/R2 **118 €** (baza 113 €, outbox #115) |

| Period | Sobe | Stara lista (1 odr.) | Nova lista (1 odr.) |
|--------|------|----------------------|---------------------|
| **2026-07-01 – 2026-08-31** | R3, R6 | 129 € | **147,00 €** |
| **2026-07-01 – 2026-08-31** | R1, R2 | 99 € | **113,00 €** |
| Ostatak godine | postojeći `RatePlanDay` | npr. 89–129 € | nepromijenjeno |

**Izuzetak — 4. 8. 2026.:** samo taj dan **sezonska baza × 4** (**452 €** R1/R2 = 113×4, **588 €** R3/R6 = 147×4). **5. 8.** = standardna sezona (**113 / 147 €**, isto kao 6. 8.). Audit: [uzorita-pricing-surge-2026-08-04-05.md](../operations/uzorita-pricing-surge-2026-08-04-05.md).

**stay.hr pohranjuje cijenu za 1 odraslog.** Channex push šalje **punu cijenu** (normal) na primary occupancy = max odrasli; Booking prikazuje smanjenja za manje gostiju.

**OBP (Channex + Booking, reduction model):**

| Postavka | Vrijednost |
|----------|------------|
| `sell_mode` | `per_person` |
| `rate_mode` | `auto` (± **5 €**) |
| Primary occupancy | **max odrasli** (R3/R6: **3** · R1/R2/R4: **2**) |
| `children_fee` | **2.00 €** |
| `meal_type` | `breakfast` |
| Booking channel | **OBP** · occupancy = primary (max odrasli) |
| Max odraslih | R1/R2/R4: 2 · R3/R6: 3 (nema 4 odraslih) |

```
normal = baza_1_osoba + (max_odrasli − 1) × 5
List   = normal − (max_odrasli − odrasli) × 5 + djeca × 2
Channex push = normal
```

| Soba | Max odr. | Normal (push) | 1 odr. | 2 odr. | 3 odr. | 3 odr. + 1 dijete |
|------|----------|---------------|--------|--------|--------|-------------------|
| **R1/R2** | 2 | **118,00** | 113 (−5) | 118 | — | 120,00 |
| **R4** (−10% od R1/R2) | 2 | **106,70** | 101,70 (−5) | 106,70 | — | 108,70 |
| **R3/R6** | 3 | **157,00** | 147 (−10) | 152 (−5) | 157 | 159,00 |

**R4 baza:** nema samostalne sezone — `RatePlanDay` = **90%** R1 (fallback R2) istog datuma, zaokruženo na cent. OBP ista kao R1 (primary **2**, Δ **€5**). Seed / refresh:

```bash
docker compose exec django python manage.py seed_uzorita_r4_rates
# optional: --dry-run · --no-push · --date-from YYYY-MM-DD · --date-to YYYY-MM-DD
```

Ponovni push iz stay.hr:

```bash
docker compose exec django python manage.py channex_ari_flush --tenant-slug uzorita
```

---

## Povezana dokumentacija

- [channex-booking-editable-settings.md](channex-booking-editable-settings.md) — što Channex može uređivati na Booking.com
- [ADR 0009 — Channel Promotions](../architecture/adr/0009-channel-promotions.md) — kanonski model, provider, outbox, drift
- [channex-uzorita-mapping.md](channex-uzorita-mapping.md) — room types, occupancy, seed naredbe
- [channel-manager-setup.md](../operations/channel-manager-setup.md) — Channex onboarding
- [booking-com-konflikt-dvostruka-rezervacija.md](../booking-com-konflikt-dvostruka-rezervacija.md) — hotel ID `4181954`
