# eVisitor — naselje poslano kao grad prebivališta (rezervacija #1159)

**Datum događaja:** 12. rujna 2026.
**Rezervacija:** #1159
**Gost:** DOMAGOJ NOVAK (guest `3246`)

---

## Sažetak

eVisitor prijava za DOMAGOJA NOVAKA pala je s greškom:

```
[[[CityOfResidence]]] 'SESVETE' [[[ne postoji u sustavu.]]]
```

Adresa gosta je bila ispravna i u dopuštenom formatu, ali je validator kao grad uzeo **naselje** (Sesvete) umjesto **jedinice lokalne samouprave** (Grad Zagreb). eVisitor šifrarnik sadrži JLS, ne naselja.

---

## Uzrok

`Guest.address` je troslojni oblik s hrvatske osobne iskaznice — *naselje, JLS, ulica i broj*:

```
SESVETE, GRAD ZAGREB, ULICA KRSTE HEGEDUSICA 13 M
```

[`validate_evisitor_residence_address`](../../backend/apps/integrations/evisitor/residence_address.py) je uzimao **prvi** segment prije zareza kao grad, pa je poslano `CityOfResidence=SESVETE`.

Cimer DARIO PREZEC (guest `3245`) ima identičan oblik adrese (`ZAGREB, GRAD ZAGREB, OPOROVEČKI VINOGRADI 66 A`) i prošao je **samo slučajno** — kod njega je prvi segment već bio `ZAGREB`. U bazi je u trenutku incidenta bilo 5 takvih adresa, pa je ovo bio latentni bug za svakog gosta čije naselje nije jednako gradu/općini (Sesvete, Dubrava, Brodarica, …).

---

## Rješenje (trajno, u kodu)

Novo determinističko pravilo `_validate_id_card_form` u validatoru: kad adresa ima ≥ 2 segmenta i **drugi** segment je `Grad X` ili `Općina X`, `CityOfResidence` se uzima iz tog segmenta.

| Adresa | Prije | Nakon |
|--------|-------|-------|
| `SESVETE, GRAD ZAGREB, ULICA KRSTE HEGEDUSICA 13 M` | `SESVETE` (odbijeno) | `ZAGREB` |
| `ZAGREB, GRAD ZAGREB, OPOROVEČKI VINOGRADI 66 A` | `ZAGREB` | `ZAGREB` |
| `Privlaka, Općina Privlaka, Ulica 5` | `Privlaka` | `Privlaka` |
| `Split, Hrvatska, Ulica 5` | `Split` | `Split` |

Uz to `_strip_grad_label` → `_strip_admin_label` (pokriva i `Općina Vodice, Ulica 1` → `Vodice`).

**Zadržana fail-closed politika (#190):** street-first adresa (`Ulica Krste Hegedušića 13, Grad Zagreb`) i dalje se odbija lokalno — naselje ne smije izgledati kao ulica da bi se pravilo primijenilo, a nema automatskog okretanja redoslijeda.

**Idempotencija:** `normalized_address` zadržava **sve** segmente, pa ponovna validacija persistirane adrese daje isti grad. To je nužno jer OCR apply i [`sync_guest_evisitor_fields`](../../backend/apps/reservations/management/commands/sync_guest_evisitor_fields.py) upisuju `normalized_address` u `Guest.address`. Zbog toga adresa gosta `3246` **nije** mijenjana u bazi — promijenio se samo izvedeni `CityOfResidence`.

---

## Ponovna prijava

```python
from apps.integrations.evisitor.service import submit_guest_checkin
from apps.reservations.models import Guest

submit_guest_checkin(Guest.objects.get(pk=3246), force_retry=True)
```

`force_retry=True` je potreban jer je `evisitor_status` bio `failed`.

---

## Povezani dokumenti i kod

| Što | Gdje |
|-----|------|
| Prihvaćeni oblici adrese | [evisitor.md — Check-in](../development/evisitor.md#prihvaćeni-oblici-adrese) |
| Prethodni incident (street-first) | [evisitor-adresa-res-190-2026-05-30.md](evisitor-adresa-res-190-2026-05-30.md) |
| Šifrarnik zemalja (isti obrazac) | [evisitor-countryofresidence-xxk-res-1019-2026-07-17.md](evisitor-countryofresidence-xxk-res-1019-2026-07-17.md) |
| Validator | `backend/apps/integrations/evisitor/residence_address.py` |
| eVisitor mapper | `backend/apps/integrations/evisitor/mapper.py` |
| Testovi | `backend/apps/integrations/tests/test_evisitor_residence_address.py` (`EvisitorIdCardAddressTests`) |

---

## Ostaje u backlogu

- **Lokalni eVisitor šifrarnik gradova** i validacija prije submita, da ops dobije jasnu grešku umjesto HTTP 400 iz eVisitora. Isti backlog kao i za zemlje (`XXK`, rez. #1019).
- `splitResidenceAddress` u [`web/booking/lib/residenceAddress.ts`](../../web/booking/lib/residenceAddress.ts) dijeli po prvom zarezu, pa za troslojnu adresu u UI-u prikazuje grad `SESVETE`. Backend je autoritet za eVisitor, pa to ne blokira prijavu.
