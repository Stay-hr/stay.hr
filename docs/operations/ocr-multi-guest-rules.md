# OCR batch — pravila za više gostiju

Operativna pravila za WhatsApp / Hospira batch OCR (`document-intake` API). Ažurirano nakon slučaja **rezervacija #51** (lipanj 2026).

**Povezano:** [id-document-import.md](../development/id-document-import.md), [ai-runbook-ocr-checkin-evisitor-2026-06.md](../operations/ai-runbook-ocr-checkin-evisitor-2026-06.md), [document-intake-telemetry.md](../development/document-intake-telemetry.md) (OCR-D KPIs, `_telemetry` contract), [ADR 0004](../architecture/adr/0004-guest-checkin-session.md) (web check-in)

**Guest web check-in (2026-07):** Gosti **ne šalju** slike dokumenata preko WhatsAppa. Jedini guest kanal: `booking.{tenant}/check-in/{token}` — ručni unos ili `WEB_GUEST` OCR upload. WhatsApp šalje link i obavijesti (`GUEST_CHECKIN_WEB_ONLY=true`).

**Document expectations (PR-C):** Broj očekivanih dokumenata i slotovi za intake dolaze isključivo iz [`document_expectations.py`](../../backend/apps/reservations/document_expectations.py) (`expected_document_count` → `expected_document_slots` → `missing_document_slots`). Completeness, WhatsApp incomplete poruke i apply_reply konzumiraju taj modul — ne računaju vlastita pravila.

---

## WEB_GUEST slot-forced matching (web check-in OCR)

Kad `DocumentIntakeJob.source == web_guest` i `guest_checkin_slot_position = N`:

1. Gost uploada slike na `POST /public/check-in/{token}/slots/{N}/documents/`.
2. OCR pipeline isti kao batch — ali matching **ne koristi** fuzzy ime / „Novi gost” dok identity ne prođe.
3. Prije slot-force: identity classify (`document_number` / MRZ na rezervaciji).
   - isti gost već ima identitet → `already_processed` (HTTP 200, bez Guest/IdDocument/face write)
   - drugi gost ima isti dokument → `duplicate_identity` (poll **HTTP 409**)
4. Ako OCR `persons[]` ima više osoba od `expected_document_count` (`expected_checkin_adults` ili OTA adults) → match `occupancy_mismatch` (`auto_apply=false`). **Nema** guest createa ni promjene occupancyja — samo job match/status. UI nudi potvrdu → `PATCH …/occupancy/` → `ensure` otvara slot → poll rematch + apply.
5. Tek ako nema collisiona i nema occupancy mismatcha, `person_index=0` se forsira na gosta na slotu `N` (dodatne OCR osobe se ne apply-aju na isti slot).
6. Poll `GET /public/check-in/{token}/jobs/{id}/` auto-apply kad je job `done` (pod `select_for_update` na rezervaciji); dok traje mismatch, apply se preskače.

Vidi [ADR 0017](../architecture/adr/0017-document-intake-identity-consistency.md).

---

## Identity confidence (matching prioritet)

Jedan redoslijed za sve intake putanje (`IDENTITY_MATCH_ORDER`):

1. `document_number` (normaliziran — `normalize_document_number`, jedina funkcija)
2. MRZ
3. OCR name + DOB
4. fuzzy name
5. empty / „Novi gost” slot

Viši nivo je **terminalan** (STOP). Prazan slot nije kandidat ako identitet već postoji na rezervaciji. Partial UNIQUE `(reservation, document_number)` na `Guest` je DB safety net; `IntegrityError` → `duplicate_identity` (nikad 500).

**Deploy / monitoring / rollback:** vidi [ADR 0017 — Deploy checklist](../architecture/adr/0017-document-intake-identity-consistency.md#deploy-checklist-operativa).

---

## Cross-tenant invariant (WABA → property tenant)

WhatsApp inbound poruke rutiraju se na WABA tenant (`platform`, npr. id 14). Rezervacija i gosti žive na property tenantu (`demo`, npr. id 1). To je **namjerno** — ne dirati routing inbound poruka.

**Invariant:** kad `DocumentIntakeJob.reservation_id` postoji → `job.tenant_id == reservation.tenant_id`.

| Entitet | Tenant |
|---------|--------|
| `WhatsAppMessage` (inbound) | WABA tenant (routing) |
| `DocumentIntakeJob`, session, image | `reservation.tenant_id` |
| Match/apply/guest lookup | `DocumentIntakeContext.effective_tenant_id` — **ne** `job.tenant_id` |

Create path mora odmah postaviti `tenant_id=reservation.tenant_id` (job, session, image). Legacy mismatch (#95) auto-heal u `DocumentIntakeContext.from_job` (jednokratno: detect → sync → log → nastavi).

---

## Što sustav radi automatski (nakon popravka)

1. **Batch OCR** — do 20 slika u jednom jobu; LLM vraća `persons[]` (uparene prednja/stražnja strana).
2. **Pre-OCR dedup** — identične slike (byte hash) se šalju LLM-u samo jednom; duplikati se bilježe u `ocr_result._preprocess.dropped_duplicate_indices`.
3. **non_document** — ugovori, pisma, članice (ADAC) označavaju se u `images[]`; ne ulaze u `persons[]`.
4. **Orphan re-OCR** — kad `ocr_under_extracted` (manje osoba nego odrasli, ili ≥2 neiskorištene slike), drugi LLM poziv samo na neiskorištene indekse.
5. **Partial apply** — prepoznati gosti se apply-aju odmah; job ostaje `DONE` dok svi odrasli nemaju dokument.
6. **Matching** — po osobi (identity confidence):
   - prvo **document_number** (terminalni hard match),
   - zatim **MRZ**,
   - zatim **ime + DOB**, zatim fuzzy ime,
   - zatim **prazan slot** — preferira **„Novi gost”** placeholder,
   - unutar batcha **ne dodjeljuje isti guest_id** dvjema osobama.
7. **auto_apply** — primjenjuje se kad je kandidat jedinstven, ili kad je cijeli batch očito na **jednu rezervaciju** (name match na primary + suputnici na istoj rezervaciji).
8. **Guest slotovi pri apply** — `max(adults_count, persons_count, broj OCR osoba)`; ako treba, kreira se dodatni **Novi gost**.
9. **Re-apply** — ponovni `POST .../apply/` na već primijenjen job **preskače** goste koji su već bili u `applied_result` (npr. samo nedostajući suputnici).

eVisitor se **ne šalje automatski** — i dalje ručno / gumb u Hospiri.

---

## Checklist recepcije (normalan flow)

1. Gost otvori web check-in link (email, WhatsApp Auto check-in, Channex).
2. Po slotu: ručni unos **ili** upload slika (WEB_GUEST OCR).
3. U eVisitor listi provjeri:
   - broj gostiju = **Osobe** na rezervaciji (`persons_count`),
   - nema **„Novi gost”** s popunjenim dokumentom negdje drugdje,
   - djeca &lt; 18: eVisitor „Nije potrebno” ako je DOB prepoznat.
4. Po potrebi **Prijavi u eVisitor** po odraslom gostu.

---

## Kad nešto fali — brza dijagnostika

```bash
docker exec stay_django python manage.py shell -c "
from apps.reservations.models import Reservation, DocumentIntakeJob
r = Reservation.objects.get(pk=REZ_ID)
print('persons', r.persons_count, 'adults', r.adults_count, 'guests', r.guests.count())
for g in r.guests.order_by('-is_primary','id'):
    print(g.id, g.name, g.document_number or '-')
j = DocumentIntakeJob.objects.order_by('-id').first()
for m in j.matches or []:
    print(m.get('person_index'), m.get('person_name'), 'auto', m.get('auto_apply'), 'guest', m.get('guest_id'))
"
```

| Simptom | Vjerojatni uzrok | Akcija |
|--------|------------------|--------|
| Samo primary ažuriran | Stari kod / apply prije process DONE | Restart django, ponovi apply na job |
| `persons` u OCR &lt; slika/2 | Loše uparivanje front/back ili miješani batch | Provjeri `unassigned_image_indices`; orphan pass ili ručni pregled |
| `unassigned_image_indices` pun | Ne-ID slike + nedostaje 2. gost u kasnim indeksima | Orphan pass automatski; recepcija vidi indekse u API `completeness` |
| Miješani batch (ID + ugovori + duplikati) | WhatsApp share više dokumenata odjednom | Dedup + orphan pass; partial apply primjenjuje prepoznate odmah |
| Nedostaje 3. gost | `persons_count` &gt; broj guest redova | Ponovi apply (kreira slot) ili `ensure_guest_slots` |
| `auto_apply=False` za sve | Više rezervacija u prozoru s praznim slotovima | Apply s ručnim `persons[]` selekcijama |

### Ponovni apply na postojeći job

```bash
docker exec stay_django python manage.py shell -c "
from apps.reservations.document_intake_service import apply_document_intake_job
from apps.reservations.models import DocumentIntakeJob
job = DocumentIntakeJob.objects.get(pk=JOB_ID)
applied = apply_document_intake_job(job.pk)
print('applied', len(applied), applied)
"
```

---

## Ručni apply s odabirom (kad je više rezervacija u prozoru)

```http
POST /api/v1/reception/document-intake/jobs/{job_id}/apply/
{
  "persons": [
    {"person_index": 0, "reservation_id": 51, "guest_id": 1057},
    {"person_index": 1, "reservation_id": 51, "guest_id": 1939},
    {"person_index": 2, "reservation_id": 51, "guest_id": NOVI_GUEST_ID}
  ]
}
```

---

## Pravila za djecu i persons_count

- Import i placeholderi: **`adults_count`** pri uvozu, **`persons_count`** pri OCR apply.
- Dijete (npr. 16 god.) **treba guest red** ako je poslao osobnu — koristi `persons_count`.
- eVisitor: &lt; 18 na dan check-ina → nije obavezno; guest red i dokument i dalje korisni za evidenciju.

---

## Miješani batch (ID + ne-ID + duplikati)

Tipičan slučaj: gost pošalje više fotografija odjednom — osobne iskaznice, ugovore o radu, članice, duplikate.

**Simptom u API / Hospiri:** `completeness.unassigned_image_indices` popunjen, `ocr_under_extracted: true`, `persons.length` &lt; `adults_count`.

**Flow:**

1. Dedup prije OCR-a (11 → 6 jedinstvenih sadržaja).
2. Pass 1: prepoznati gosti + `non_document` oznake.
3. Orphan pass na neiskorištenim indeksima (npr. sort 7–8 → drugi gost).
4. Partial apply prepoznatih; incomplete poruka gostu samo za nedostajuće.
5. `reconcile_guest_document_batches` ponovo pokreće `process_document_intake_job` kad je `ocr_under_extracted`.

---

## Datoteke u kodu

| Datoteka | Uloga |
|----------|--------|
| `backend/apps/reservations/document_intake_match.py` | Matching + auto_apply |
| `backend/apps/reservations/document_intake_preprocess.py` | Byte-hash dedup prije OCR |
| `backend/apps/reservations/document_intake_completeness.py` | `unassigned_image_indices`, `ocr_under_extracted` |
| `backend/apps/ai/document_ocr.py` | Batch OCR + orphan pass |
| `backend/apps/reservations/guest_slots.py` | `ensure_guest_slots_for_intake()` |
| `backend/apps/reservations/document_intake_service.py` | process / apply (partial mode) |
| `backend/apps/integrations/whatsapp/document_intake_finalize.py` | Partial apply + incomplete poruka |
| `backend/apps/api/reception_document_intake_views.py` | API |

Nakon promjena Python koda: `./scripts/deploy.sh` ili `docker compose restart django`.

### eVisitor — obavezna polja

| Polje | Uvjet |
|-------|--------|
| **Spol** | Obavezan za odrasle. Njemačka osobna **nema spol** na kartici ni u MRZ (`<`) — unesite **M/F ručno** u Hospiri prije prijave. |
| **Adresa** | Format **`Grad, ulica`** (bez poštanskog broja u gradu). Npr. `94036 PASSAU, ...` → `PASSAU, WILHELM-PÖLL-STRAGE 7`. |

Sync nakon OCR:

```bash
docker exec stay_django python manage.py sync_guest_evisitor_fields --reservation-id 51
```

Spol iz MRZ-a (kad postoji M/F) i normalizacija adrese rade automatski pri novom OCR apply-u.

### Avatar (face crop)

Automatski crop koristi **OpenCV face detection** na prednjoj strani osobne; LLM `face_bbox` se ignorira ako je generički placeholder. Ulazne fotke prolaze kroz **EXIF orientation** prije detekcije (phone JPEG s Orientation=6 inače daje krivi crop).

Ponovni crop postojećih gostiju:

```bash
docker exec stay_django python manage.py recrop_guest_faces --reservation-id 51
docker exec stay_django python manage.py recrop_guest_faces --guest-id 1057 --dry-run
```
