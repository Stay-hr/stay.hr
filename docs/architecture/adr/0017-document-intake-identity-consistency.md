# ADR 0017: Document intake identity consistency

## Status

Accepted (2026-08) — implemented with Danijela double-upload incident (jobs #167/#168 → G#2021) as the proving case.

## Summary

**Why:** Cross-job upload of the same identity document could match an empty „Novi gost” slot because `document_number` matching was not terminal and WEB_GUEST slot-force did not check existing identities on the reservation.

**How:** Explicit identity confidence order; collision classify (`already_processed` / `duplicate_identity`); face/IdDocument write ban on collision; reservation `select_for_update` + `IntegrityError` → `duplicate_identity`; MRZ consistency abort on apply; partial UNIQUE on `Guest(reservation, document_number)`.

## Core invariants

- One identity may belong to only one `Guest` within a reservation.
- `document_number` hard match is terminal (priority 1 in `IDENTITY_MATCH_ORDER`).
- `duplicate_identity` / `already_processed` never create a new `Guest`.
- Those outcomes never mutate `IdDocument.face_photo` (nor any future FaceBinding / FaceEmbedding path).
- Apply never writes MRZ-inconsistent OCR fields (`mrz_inconsistent`).
- `IntegrityError` on the document UNIQUE maps to `duplicate_identity` — never HTTP 500.
- Sole normalizer: `normalize_document_number()` (match, collision, migration, PATCH, apply storage).

## Observability

- Audit log: `DOCUMENT_DUPLICATE_DETECTED reservation_id=… job_id=… existing_guest_id=… target_guest_id=… document_number=… reason=…`
- Counters (log-backed): `identity.already_processed`, `identity.duplicate`, `identity.mrz_inconsistent`, `identity.hard_match.document`, `identity.hard_match.mrz`

## Deploy checklist (operativa)

### 1. Prije migrate `0033`

Na ciljnoj bazi (produkcija) potvrditi da nema konflikata koje cleanup ne može riješiti:

```sql
SELECT reservation_id,
       UPPER(REGEXP_REPLACE(document_number, '[^A-Za-z0-9]', '', 'g')) AS doc_norm,
       COUNT(*) AS n,
       ARRAY_AGG(id ORDER BY is_primary DESC, id) AS guest_ids
FROM reservations_guest
WHERE document_number <> ''
GROUP BY 1, 2
HAVING COUNT(*) > 1;
```

Očekivano: **0 redova**. Ako ima redova — ručno razriješi (zadrži primary / najstarijeg, blankiraj `document_number` na ostalima) prije migratea.

Migracija `0033` sama kanonizira brojeve i blankira preostale duplikate (primary, zatim najniži `id`); partial UNIQUE se dodaje nakon cleanup-a.

**Provjera 2026-08-04 (dedicated-hel1):** 0 duplikata.

### 2. Monitoring nakon deploya (prvih nekoliko dana)

```bash
# na django / celery hostu
docker compose logs -f django celery-worker 2>&1 | rg 'DOCUMENT_DUPLICATE_DETECTED|identity_metric name=identity\.(duplicate|already_processed|mrz_inconsistent)'
```

Tumačenje:

| Signal | Očekivano | Akcija ako skoči |
|--------|-----------|------------------|
| `identity.already_processed` | ponekad (re-upload istog slota) | niska — idempotentno |
| `identity.duplicate` | rijetko (gost pokuša isti doc na drugi slot) | potvrdi UX 409; nema novog Guest/face |
| `identity.mrz_inconsistent` | rijetko (loš OCR vs MRZ) | pregled joba; bez Guest write |
| `DOCUMENT_DUPLICATE_DETECTED` | prati gore navedeno | forenzika po `reservation_id` / `job_id` |

### 3. Rollback

| Sloj | Akcija |
|------|--------|
| App image | Vrati prethodni django image + `docker compose up -d django celery-worker celery-beat` |
| Migracija `0033` | **Ne rollbackaj automatski.** Partial UNIQUE je kompatibilan sa starim kodom (samo sprječava nove duplikate). Ako baš treba skinuti constraint: `ALTER TABLE reservations_guest DROP CONSTRAINT reservations_guest_unique_doc_per_reservation;` — nakon eksplicitne odluke, ne kao dio image rollbacka. |
| Feature | Nema feature flaga; rollback = prethodni image. Identity classify nestaje s imageom; UNIQUE ostaje kao DB safety net. |

## References

- Runbook: [`docs/operations/ocr-multi-guest-rules.md`](../../operations/ocr-multi-guest-rules.md)
- Code: `document_intake_identity.py`, `document_intake_match.py`, `document_intake_web_guest.py`
- Migration: `reservations/0033_guest_unique_document_per_reservation.py`
