# Operator document intake — audit / recovery checklist

Kako provjeriti WhatsApp slike od operatora (Toni) za zadani datum, pokrenuti OCR + apply + eVisitor + check-in kad treba, i **ne dirati** već završene (checked-out) rezervacije.

**Povezano:** [whatsapp-operator-toni-runbook.md](whatsapp-operator-toni-runbook.md) (dnevni flow za Tonija).

**Operator (primjer):** Toni Šupe `wa_id=385998388513`  
**Tenant:** Uzorita (`tenant-id` / slug `uzorita`, property tenant `2`; WABA poruke žive na platform tenant)

---

## Pravilo (invariant)

| Stanje rezervacije | Što raditi |
|--------------------|------------|
| `confirmed` / `checked_in` / dolazak danas | OCR → poveži → update gosta → eVisitor → check-in (ako još nije) |
| **`checked_out`** | **Ne dirati.** Ne replay `complete_operator_document_job`, ne merge/apply, ne eVisitor, ne “heal” dokumenata. |
| Job s OCR osobama koje mapiraju samo na checked-out rezervacije | Ostavi job; dokumentiraj u bilješci ako treba — bez apply |

Maloljetnici **idu** u eVisitor (`guest_requires_evisitor` je True za sve goste). `TTPaymentCategory` za djecu je `"1"` / `"2"` prema dobi na `check_in`; odrasli zadržavaju config default.

---

## 1. Pronađi inbound slike za datum

One-shot shell (profil `test-run` / `django-run` — bez Traefik labova):

```bash
docker compose --profile test-run run --rm django-run python manage.py shell -c "
from datetime import datetime
from zoneinfo import ZoneInfo
from apps.integrations.models import WhatsAppMessage

zagreb = ZoneInfo('Europe/Zagreb')
# prilagodi datume
start = datetime(2026, 7, 15, 0, 0, tzinfo=zagreb)
end = datetime(2026, 7, 17, 0, 0, tzinfo=zagreb)

msgs = WhatsAppMessage.objects.filter(
    created_at__gte=start, created_at__lt=end,
    wa_id='385998388513', direction='inbound', message_type='image',
).order_by('created_at')
print(f'count={msgs.count()}')
for m in msgs:
    print(f'#{m.pk} {m.created_at.astimezone(zagreb):%Y-%m-%d %H:%M} type={m.message_type}')
"
```

Zabilježi `message` pk-ove (npr. `#1272`–`#1290`).

---

## 2. Mapiraj na operator session / DocumentIntakeJob

```bash
docker compose --profile test-run run --rm django-run python manage.py shell -c "
from datetime import datetime
from zoneinfo import ZoneInfo
from apps.reservations.models import (
    DocumentIntakeJob, DocumentIntakeJobSource, WhatsAppOperatorSession,
)

zagreb = ZoneInfo('Europe/Zagreb')
since = datetime(2026, 7, 15, 0, 0, tzinfo=zagreb)

print('=== sessions ===')
for s in WhatsAppOperatorSession.objects.filter(
    operator_wa_id='385998388513', updated_at__gte=since,
).select_related('job').order_by('-updated_at'):
    print(f'session#{s.pk} status={s.status} job={s.job_id} imgs={s.job.images.count() if s.job_id else 0}')

print('=== jobs ===')
for j in DocumentIntakeJob.objects.filter(
    source=DocumentIntakeJobSource.WHATSAPP_OPERATOR, created_at__gte=since,
).prefetch_related('images').order_by('-pk'):
    print(f'job#{j.pk} status={j.status} res={j.reservation_id} tenant={j.tenant_id} imgs={j.images.count()}')
    for img in j.images.all().order_by('pk'):
        print(f'  {img.image.name if img.image else None}')
"
```

Imena datoteka tipa `op_1272.jpg` odgovaraju `WhatsAppMessage` pk.

---

## 3. Odluči: gotovo vs treba recovery

Za svaki job:

1. `status=done` + `applied_result` popunjen + povezane rezervacije `checked_out` ili `checked_in` s eVisitor OK → **ništa**.
2. `status` nije `done`, ili `applied_result` prazan, ili `reservation_id` null, a OCR osobe pripadaju **aktivnoj** rezervaciji → recovery (dolje).
3. OCR osobe pripadaju samo **checked_out** rezervacijama → **ne apply**; ostavi.

Provjera gostiju / eVisitor:

```bash
docker compose --profile test-run run --rm django-run python manage.py shell -c "
from apps.reservations.models import Guest, Reservation
from apps.integrations.evisitor.summary import evisitor_summary_for_reservation, evisitor_progress_for_reservation

for rid in [852, 1014]:  # prilagodi
    r = Reservation.objects.get(pk=rid)
    print(f'res#{rid} status={r.status} ev_summary={evisitor_summary_for_reservation(r)!r} progress={evisitor_progress_for_reservation(r)}')
    for g in Guest.objects.filter(reservation_id=rid).order_by('pk'):
        print(f'  g#{g.pk} {g.name!r} doc={g.document_number!r} ev={g.evisitor_status!r}')
"
```

---

## 4. Recovery (samo aktivne rezervacije)

### Dry-run pa apply — jedan job

```bash
docker compose --profile test-run run --rm django-run python manage.py complete_operator_document_job \
  --job-id <JOB_ID> --reservation-id <RES_ID> --operator-wa-id 385998388513 --dry-run

docker compose --profile test-run run --rm django-run python manage.py complete_operator_document_job \
  --job-id <JOB_ID> --reservation-id <RES_ID> --operator-wa-id 385998388513
```

### Više sesija / rascijepani batch

```bash
docker compose --profile test-run run --rm django-run python manage.py reconcile_operator_sessions \
  --tenant-slug uzorita --operator-wa-id 385998388513 \
  --since 2026-07-15 --reservation-id <RES_ID> --dry-run

# bez --dry-run tek kad je RES aktivna (nije checked_out)
```

**Prije apply:** potvrdi `Reservation.status != checked_out`.

---

## Primjer: 2026-07-15 / 2026-07-16 (Toni)

| Batch | WA poruke | Job | Rezultat |
|-------|-----------|-----|----------|
| 15.07. ~20:46 | `#1272`–`#1284` (13 slika) | `#122` | OCR 6 osoba → apply **#852** (Pereira) + **#1014** (Krapane); check-in + eVisitor odraslih; kasnije checkout |
| 16.07. ~09:07 | `#1285`–`#1290` (6 slika) | `#123` | OCR 3 osobe → apply **#105** (Pignat/Ejarque); check-in + eVisitor; checkout |

Outbound potvrde Toniju (~14:23–14:25 CEST 16.07.): „Check-in obavljen…” za `#852`, `#1014`, `#105`. Kasniji klik **Da** → „Check-in za ovu seriju je već obavljen.” — očekivano, **ne replay**.

**Što nismo dirali:**

- Sve tri rezervacije nakon `checked_out`
- Job `#121` (slike od **14.07.**, OCR bez uspješnog matcha / djelomično checked-out gosti) — ostavljen

**eVisitor napomena (#1014):** Elina (10) i Sofija (17) — `requires=False`; samo Dana + Ainars u eVisitoru. Summary `checked_out` + progress `required=2, sent=2` je OK.

---

## Brzi checklist (kopiraj)

```
[ ] 1. Lista inbound image WA poruka za datum + operator_wa_id
[ ] 2. Sessioni / jobovi / broj slika / reservation_id
[ ] 3. Za svaki job: applied_result + Reservation.status
[ ] 4. Ako checked_out → STOP (ne complete / ne reconcile apply)
[ ] 5. Ako aktivna + nedovršeno → dry-run complete_operator_document_job, pa apply
[ ] 6. Provjeri evisitor_summary / progress (maloljetnici također required; TT 1/2)
[ ] 7. Ne šalji gostu notifikaciju ako nije potrebno (--guest-notify skip)
```

---

## Command reference

| Command | Namjena |
|---------|---------|
| `complete_operator_document_job` | Replay OCR match + apply + check-in + eVisitor + WA potvrda operatoru |
| `reconcile_operator_sessions` | Merge rascijepanih operator sesija/jobova, zatim complete |
| `seed_uzorita_whatsapp_operators` | Whitelist Tonija / Ante |

One-off manage.py: uvijek **`django-run`** (`--profile test-run`), ne `docker compose run django` (Traefik 502 rizik).
