# Multi-room i overbooking — operativni checklist

**Objekt:** Luxury Room Uzorita B&B (`uzorita`, tenant #2)  
**Kanal:** Channex → Booking.com (property ID `4181954`)

Ovaj runbook sprječava tip overbookinga iz 2026.: PMS ima manje soba nego Booking.com → Channex ne zatvara sve listinge → B.com prodaje istu noć dvaput.

---

## Kad koristiti

- Nova rezervacija s **2+ soba** ili PDF s više `Luxury Room Uzorita - R*`
- Rezervacija „cijeli objekt“ (4 sobe: R1, R2, R3, R6)
- Nakon **PDF importa** koji mijenja dodjelu soba
- Dnevno (automatski): Celery `detect_overbooking` (06:00) + `detect_multi_room_gaps` (06:15) + `verify_channex_availability` (06:30, **verify-only** / notify; repair samo ručno `--repair` na hel1 writeru — [ADR 0014](../architecture/adr/0014-channex-outbound-guard.md), [incident 2026-08-01](incidents/2026-08-01-wsl-channex-second-writer-overbooking.md))
- WSL nikad ne smije biti drugi Channex writer (`CHANNEX_OUTBOUND_ENABLED=false`)
- Nakon **root cause** tipa second-writer / krivih Channex writeova: obavezan **full inventory reconcile** na hel1 + **Verify clean (0 mismatches)** prije **Incident closed** (Phase B / residual exposure — [incident 2026-08-01](incidents/2026-08-01-wsl-channex-second-writer-overbooking.md#after-bad-outbound-writes--close-procedure))

---

## Checklist — isti dan kad stigne rezervacija

1. **Booking.com PDF** (extranet → Print confirmation) spremi u `.imports/` ili importaj u Reception / CLI.
2. U stay.hr provjeri:
   - `units_count` = broj soba na PDF-u
   - `ReservationUnit` — svaka soba ima `unit` (R1, R2, R3, R6), ne samo jedna
   - `import_source=booking_pdf` za autoritativne multi-room (Channex ne smije smanjiti broj soba)
3. **Channex inventar** (automatski nakon PDF importa u stay.hr od 6/2026.; ručno ako treba):
   ```bash
   docker compose exec django python manage.py channex_ari_full_sync --tenant-slug uzorita
   ```
4. **Verify live Channex availability** (GET vs stay.hr; re-push on mismatch):
   ```bash
   docker compose exec django python manage.py verify_channex_availability --tenant-slug uzorita
   # writer (hel1) only — explicit repair after reading output / threshold:
   # docker compose exec django python manage.py verify_channex_availability --tenant-slug uzorita --repair
   ```
5. **Detekcija konflikta**:
   ```bash
   docker compose exec django python manage.py detect_overbooking --tenant-id 2 --from-date YYYY-MM-DD
   ```
6. **Multi-room inventar gapovi** (nepotpuni `ReservationUnit` / Channex calendar mismatch):
   ```bash
   docker compose exec django python manage.py detect_multi_room_gaps --tenant-id 2 --from-date YYYY-MM-DD
   ```
7. Ako `detect_overbooking` > 0 ili reception push „Overbooking“ — **ne check-in** kasnijeg gosta na zauzetu sobu; otkaz / relocacija prema [booking-com-konflikt](booking-com-konflikt-dvostruka-rezervacija.md) uzorcima.

---

## Reconcile (tjedno ili pri sumnji)

```bash
docker compose exec django python manage.py reconcile_booking_units --tenant-id 2
```

Prikazuje rezervacije gdje `units_count` ili Channex `rooms[]` ne odgovara broju `ReservationUnit` u stay.hr.

---

## Channex — Messaging i Booking CRS (ručno u Channex UI)

Bez ovoga API vraća **403** za poruke i otkaz rezervacije:

| App | Svrha |
|-----|--------|
| **Messaging & Reviews** | `send_channex_booking_message` → B.com Poruke |
| **Booking CRS** | `cancel_channex_booking` → otkaz na kanalu |

Vidi [channex-uzorita-booking-channel.md](../integrations/channex-uzorita-booking-channel.md).

Email na `@guest.booking.com` (tenant SMTP) radi i bez Messaging appa; extranet „Poruke“ može ostati prazan.

---

## Lipanj 2026. — R1 overbooking 4. 6. (Helmuth / Wolfgang)

- Konflikt riješen operativno: Helmuth (#814) → **canceled**, smješten ParkCity; Wolfgang (#106) → R1+R2 check-in Uzorita.
- stay.hr usklađen 5. 6.: Wolfgang R1+R2, Kees (#70) R1+R2, Lada (#159) R2+R3+R6 — svi `booking_pdf` lock.
- Vidi [situacija-lipanj-2026-r1-helmuth-overbooking.md](situacija-lipanj-2026-r1-helmuth-overbooking.md).

## Lipanj 2026. — R1 overbooking 5. 6. (Daniela / Kees)

- Konflikt riješen operativno: Daniela (#837) → **ParkCity**, uklonjena s R1 u stay.hr; Kees (#70) → R1+R2 check-in Uzorita.
- `detect_overbooking` R1 5.–6. 6.: **0** (ostaju srpanjski R2/R3 konflikti).
- Vidi [situacija-lipanj-2026-r1-daniela-overbooking.md](situacija-lipanj-2026-r1-daniela-overbooking.md).

## Lipanj 2026. — XLS audit + PDF import (5.–30. 6.)

Izvor: `.imports/Check-in 2026-06-05 to 2026-06-30.xls` (39 redova, 33 aktivne).

| Booking | Gost | PDF | stay.hr (nakon importa) |
|---------|------|-----|-------------------------|
| **6748210815** | Jerzy Mochnik (#73) | R1+R3, €159,80, 5 odraslih | R1+R3 `booking_pdf` lock |
| **6109473116** | Natascha Theußl (#130) | R1+R2, €309,30, 2 odrasla | R1+R2 `booking_pdf` lock |

- Veble #786 (6860885586): namjerna relokacija R1→R2 (Channex poruka 26. 5.) — ne dirati.
- `detect_overbooking` od 25. 6.: **0 lipanjskih** konflikata (srpanj Lada/David/Sandy ostaje).

---

## Srpanj 2026. — XLS audit + Channex ARI (5. 6.)

Izvor: `.imports/Check-in 2026-07-01 to 2026-07-31.xls` (47 redova, 40 aktivnih).

| Booking | Gost | Sobe (XLS = stay.hr) | Akcija 5. 6. |
|---------|------|----------------------|-------------|
| **5976910280** | Pino (#801) | R1+R3 | Channex ARI push |
| **5882457664** | Kukla (#795) | R1+R6 | Channex ARI push |
| **5865972471** | Lada (#159) | R2+R3+R6 | Channex ARI push (PDF lock) |
| **5796838012** | Susanne (#82) | 4 sobe | Channex ARI push (PDF lock) |

- `channex_ari_full_sync uzorita` — 5. 6. 2026.
- Madrigal #56 (5679320966): XLS `cancelled_by_guest` → stay.hr `canceled`
- Wiśniewski #690: operativno R2; email gostu (PL) 5. 6.
- Sandy/David/Eduardo: B.com support mail poslan — čeka extranet otkaz

## Kolovoz 2026. — R2 overbooking 15.–16.8. (Philippe / Nikola)

| Booking | Gost | Status 5. 6. |
|---------|------|--------------|
| **6104960555** | Philippe (#708) | PDF import R2+R6, `booking_pdf` lock — check-in |
| **6911389256** | Nikola (#831) | **canceled** (`6911389256-canceled.pdf`) |

### Preventiva (od 5. 6. 2026.)

- Channex ingest: upozorenje ako `rooms=0` ili 1 soba + 4+ odraslih (`MULTI_ROOM_SUSPECT`)
- `flag_channex_room_mismatch`: automatski ARI push kad stay.hr ima 2+ sobe, Channex manje
- Dnevni scan `detect_multi_room_gaps`: unit gapovi + Channex calendar mismatch
- **Od 10. 9. 2026.:** zatvaranje ostatka objekta pali se samo kod nekonzistentnog / suspect multi-rooma — vidi [Property-close pravilo](#property-close-pravilo-konzistentan-vs-suspect-multi-room)

---

## Property-close pravilo: konzistentan vs suspect multi-room

**Problem do 10. 9. 2026.:** `qualifies_for_whole_property_sync` palio je na **svaku** rezervaciju s 2+ mapirane sobe iz `{R1, R2, R3, R6}`, pa je i ispravno mapiran 2-sobni booking blokirao ostatak objekta. U prozoru 9.–14. 9. 2026. to je bez gosta držalo zatvorenih 6 soba-noći; cleanup istog dana otvorio je ukupno 31 soba-noć.

Pravilo sada:

| Stanje rezervacije | Konkurentni listinzi |
|---|---|
| kanal traži N soba i stay.hr drži točno N različitih soba, bez warning notea | **ostaju otvoreni** — sobe su zatvorene preko okupiranosti |
| `units_count` je `None` ili 0 (kanal nije dao pouzdan broj) | zatvaraju se |
| broj mapiranih soba ≠ `units_count`, u bilo kojem smjeru | zatvaraju se |
| `MULTI_ROOM_SUSPECT:` / `CHANNEX_EMPTY_ROOMS:` / `CHANNEX_ROOMS_MISMATCH:` u `notes` | zatvaraju se |

Invariant konzistentnosti (`multi_room_assignment_is_consistent`): `units_count > 0` **i** broj **različitih** `unit_id` na `ReservationUnit` je točno `units_count` **i** nema otvorenog room warninga. Broje se različite sobe, ne redovi — baza ne brani dva `ReservationUnit` reda na istu sobu, pa bi brojanje redova propustilo duplikat kao „dvije sobe”.

Zaštita od overbookinga se time ne smanjuje: `force_close_property_channex_availability` i dalje zatvara sve listinge kod `MULTI_ROOM_SUSPECT` / `CHANNEX_EMPTY_ROOMS` / rooms mismatch. Mijenja se samo to da zdrava, potpuno mapirana rezervacija više ne aktivira zaštitni mehanizam.

---

## Cleanup zaostalih property-close blokada

`prune_property_close_blocks` briše `property-close:` blokade koje po pravilu iznad više nemaju razlog i **ponovno izračuna** dostupnost preko `compute_unit_availability`. Nikad ne upisuje `availability=1` zato što je blokada obrisana — drži li tu noć druga rezervacija ili druga blokada, ostaje 0.

Blokada se briše kad rezervacija više ne postoji, kad je izvan aktivnih statusa (sve osim `pending` / `expected` / `checked_in`) ili kad je multi-room postao konzistentan. Inače se zadržava uz ispisan razlog. Uz `--reservation-id` može se ograničiti na jednu rezervaciju.

### 1. Dry-run (default)

```bash
docker compose exec django python manage.py prune_property_close_blocks \
  --tenant-slug uzorita --from-date 2026-09-09
```

Ispis ima dvije sekcije:

- **A)** svaka `property-close` blokada u rasponu, s odlukom `delete` / `keep` i razlogom
- **B)** noći koje bi trebale biti zatvorene a stvarno su otvorene (`WARN missing property-close`)

Sekcija B gleda **dostupnost**, ne evidenciju blokada: nedostatak blokade je izloženost samo ako `compute_unit_availability` još vraća > 0. Dvije rezervacije koje zajedno drže sve četiri core sobe nisu gap.

Bez `--to-date` raspon ide od `--from-date` unaprijed, bez horizonta. To je namjerno: ručni cleanup 10. 9. 2026. s horizontom od 180 dana promašio je 7 blokada u lipnju 2027. (26 soba-noći).

Gate prije `--apply`: svaki `delete` mora biti objašnjiv, a sekcija B prazna. Ako nije — stop; `--apply` će i sam odbiti raditi.

### 2. Apply

```bash
docker compose exec django python manage.py prune_property_close_blocks \
  --tenant-slug uzorita --from-date 2026-09-09 --apply
```

Dry-run **nije** input za `--apply`: komanda iznova čita i ocjenjuje svaku blokadu pod `select_for_update`, pa rezervacija nastala između dva poziva ne može biti pogođena zastarjelom odlukom. Redoslijed je delete → lokalni recompute i outbox → commit → jedan remote ARI push.

Na writer hostu (`CHANNEX_OUTBOUND_ENABLED=true`) `--apply` stvarno ponovno otvara listinge na Booking.comu; na read-only hostu komanda odbija raditi ([ADR 0014](../architecture/adr/0014-channex-outbound-guard.md)).

### 3. Provjera nakon apply-a

```bash
docker compose exec django python manage.py prune_property_close_blocks \
  --tenant-slug uzorita --from-date 2026-09-09 --check
docker compose exec django python manage.py verify_channex_availability --tenant-slug uzorita
docker compose exec django python manage.py detect_overbooking --tenant-id 2 --from-date 2026-09-09
docker compose exec django python manage.py detect_multi_room_gaps --tenant-id 2 --from-date 2026-09-09
```

`--check` je read-only i vraća exit 1 ako postoji ijedna obsolete blokada **ili** ijedna nezaštićena noć. Očekivano: exit 0, `mismatches=0`, `detect_overbooking` 0.

### Recovery kad remote push padne

Komanda tada završi s exit 1 i porukom `Do NOT re-run this command`. DB stanje je **već commitano i ispravno**, pa se cleanup **ne ponavlja**. Pogođeni `ChannexAriOutbox` red je u statusu `FAILED`, a flush bira samo `PENDING` redove — ni ponovni `channex_ari_flush` ga ne bi pokupio. Ponavlja se samo ARI sync:

```bash
docker compose exec django python manage.py channex_ari_full_sync --tenant-slug uzorita
docker compose exec django python manage.py verify_channex_availability --tenant-slug uzorita
```

Nakon toga `--check` potvrđuje da su blokade već obrisane. `FAILED` red ostaje u outboxu i nakon uspješnog full synca; njegovo terminalno zatvaranje opisano je u [channel-manager-setup.md — Disposition za `FAILED` ARI outbox redove](channel-manager-setup.md#disposition-za-failed-ari-outbox-redove).

---

## Otvoreni B.com otkazi (stanje 5. 6. 2026.)

**Zatvoreno na B.com:** Pierre **5238895494** (#798) — `cancelled_by_guest` (XLS srpanj), stay.hr `canceled`.

Još čeka extranet/support otkaz:

| Booking | Gost | Dokument |
|---------|------|----------|
| 5398124917 | Eduardo de las Heras | [booking-com-konflikt-2026-07-24-overbooking.md](../booking-com-konflikt-2026-07-24-overbooking.md) — **NE check-in** (Susanne #82), support mail 5. 6. |
| 5461475045 | Sandy Bowser | [situacija-srpanj-2026-r2-r3-overbooking.md](situacija-srpanj-2026-r2-r3-overbooking.md) — **NE check-in R3** |
| 6754897669 | David Martín Céspedes | isto — **NE check-in R2** |

---

## Povezani dokumenti

- [situacija-lipanj-2026-r1-helmuth-overbooking.md](situacija-lipanj-2026-r1-helmuth-overbooking.md)
- [situacija-srpanj-2026-r2-r3-overbooking.md](situacija-srpanj-2026-r2-r3-overbooking.md)
- [booking-com-konflikt-2026-07-24-overbooking.md](../booking-com-konflikt-2026-07-24-overbooking.md)
- [situacija-svibanj-2026-r3-r6-overbooking.md](situacija-svibanj-2026-r3-r6-overbooking.md)
