# WhatsApp pred-check-in — predložak

Operativni predložak za traženje dokumenata i slanje detalja dolaska **prije** check-ina. U Hospiri oba koraka koriste **postojeći Generiraj** (deterministički template, bez LLM). Zatim u zasebnoj poruci priložiti sliku ulaza.

**Slika ulaza:** `backend/assets/whatsapp/uzorita_entrance.jpg` (znak „Restaurant UZORITA”, pločica 58 / 4★, crna kapija s lozom; ažurirano 2026-07-22) — automatski se šalje nakon post-check-in auto-odgovora na parking/dolazak

**Povezano:** [ai-runbook-ocr-checkin-evisitor-2026-06.md](./ai-runbook-ocr-checkin-evisitor-2026-06.md), [id-document-import.md](../development/id-document-import.md)

**Backend:** `backend/apps/communications/guest_compose.py`, `guest_language_resolver.py`

**Izvor teksta:** `Property.guest_info` (JSON) — parking, ulaz, dokumenti, post-check-in odgovori. Opcionalni ključ `canonical_language` (ISO 639-1, default `en`) određuje bazni jezik predloška kad gostov reply jezik nema lokalizirani tekst u `guest_info`. Uzorita seed:

```bash
python manage.py seed_uzorita_guest_info --tenant-slug uzorita --property-slug uzorita
```

Schema i helperi: `backend/apps/properties/guest_info.py`, podaci: `backend/apps/properties/uzorita_guest_info.py`.

**Flutter chat (timeline + tri kanala):** [guest-messages-flutter.md](../development/guest-messages-flutter.md)

**Ops — web check-in vs WhatsApp welcome:** Ako rezervacija već ima `GuestCheckInSession` sa `status=completed` (web obrazac završen — Channex, email, recepcija ili WA link), beat **ne šalje** Meta welcome template ni intro email tog dana (`reason=web_checkin_completed`). Aktivna / ready sesija ne blokira.

**Ops — late Channex booking (check-in unutar open window):** Na `Reservation` create s `import_source=channex`, ako je property-local `check_in` unutar `guest_checkin_opens_days_before` dana (default 7; uključivo D-7 i D-0), Celery task odmah šalje web check-in link na Booking/Channex chat (`communications.maybe_send_immediate_channex_checkin_link`). Send ide kroz `send_guest_checkin_link_via_channex` s `select_for_update` claimom na sessionu (atomska zaštita od duplikata). D-N reminder i ručni ponovni poziv vraćaju `already_distributed` / `link_already_distributed` ako je link već distribuiran.

---

## API (Hospira)

| Korak | Hospira UI | Compose body |
|-------|------------|----------------|
| Poruka 1 — prije dolaska | Check-in → **Generiraj** | `{"intent": "checkin"}` |
| Poruka 2 — nakon OCR apply | **Isti** Reply → **Generiraj** (bez novog gumba) | `{"intent": "reply", "hint": "checkin ready"}` |
| Ostali reply | Reply → Generiraj | `{"intent": "reply"}` → LLM ili generički fallback |

Endpoint: `POST /api/v1/reception/reservations/{id}/messages/compose/`

Oba check-in koraka vraćaju `llm_used: false`. **Ista pravila jezika** za poruku 1 i poruku 2 (tablica ispod); opcionalno `"language": "de"` override.

### Flutter (hospira_flutter)

- Nakon uspješnog **OCR apply** postavi lokalni flag na rezervaciji.
- Sljedeći **Reply → Generiraj** automatski šalje `hint: "checkin ready"` u pozadini — recepcija ne vidi hint.
- Nakon slanja poruke (ili izlaska iz OCR flowa) resetiraj flag; sljedeći Reply opet normalan.
- **Ne** šalji posebnu jezičnu logiku za poruku 2 — isti compose bez `language` (backend mapira iz `booker_country` / nacionalnosti).

---

## Odabir jezika (automatski)

Centralizirano u `GuestLanguageResolver` (`backend/apps/communications/guest_language_resolver.py`).

### Proactive (check-in prije poruke gosta)

Prioritet: API `language` → `booker_country` / nacionalnost → tenant/property default → **en**.

| Template | Države (ISO2) |
|----------|----------------|
| **hr** | HR, RS, BA, ME, SI, MK |
| **de** | DE, AT, CH, LI |
| **es** | ES, MX, AR, CO, CL, PE, VE, EC, UY, PY, BO, CR, PA, DO, GT, HN, NI, SV, CU, PR |
| **fr** | FR, MC, LU |
| **it** | IT |
| **pl** | PL |
| **ro** | RO |
| **nl** | NL |
| **cs** | CZ |
| **hu** | HU |
| **pt** | PT |
| **el** | GR |
| **sk** | SK |
| **en** | sve ostalo |

Podržani template jezici u `guest_info`: **hr, en, de, es, fr, sk**. Ostali reply jezici koriste `canonical_language` + LLM prijevod.

### Reactive (odgovor na poruku gosta)

Prioritet: API override → LLM `reply_language` → detekcija iz inbound poruke → spremljeni conversation language → country → tenant default → **en**.

Inbound poruke ažuriraju `conversation_language` na thread state kad je confidence ≥ 0.65 (kratki odgovori poput „OK” ili 👍 ne mijenjaju jezik).

---

## Metapodaci (zaglavlje datoteke)

```
KANAL: WhatsApp Business
Booking: {BOOKING_KOD}
Tel.: {TELEFON}
Jezik: {JEZIK}
Prilog: slika ulaza → backend/assets/whatsapp/uzorita_entrance.jpg
```

---

## Placeholderi

| Placeholder | Primjer |
|-------------|---------|
| `{IME}` / `{NAME}` | Wolfgang / Florin |
| `{BOOKING_KOD}` | 5036489024 |
| `{SOBA}` | Deluxe King Room R1 + R2 |
| `{CHECK_IN}` | 4. 6. 2026. |
| `{CHECK_OUT}` | 5. 6. 2026. |
| `{CHECK_IN_SAT}` | 15:00 |
| `{CIJENA}` | 180,15 |
| `{VALUTA}` | € |
| `{BROJ_ODRASLIH}` | 2 |
| `{PLAĆANJE}` | vidi tablicu ispod |

**Adresa (fiksno):** Ul. bana Josipa Jelačića 58, 22000 Šibenik  
**Maps:** https://maps.app.goo.gl/BN15CcMmmAapmjUs7

---

## Plaćanje — guest-friendly tekstovi

Logika usklađena s `resolve_payment_method()` u `backend/apps/billing/services/payment.py`. Za gosta **ne** koristiti fiskalni tekst „TRANSAKCIJSKI RAČUN”.

### Booking.com (Payments by Booking.com)

| Jezik | Tekst |
|-------|-------|
| **HR** | Ukupna cijena: {CIJENA} € — plaćeno u cijelosti putem Booking.com (Payments by Booking.com). Boravak ne plaćate na check-inu. |
| **EN** | Total price: €{CIJENA} — paid in full via Booking.com (Payments by Booking.com). No payment due for your stay at check-in. |
| **DE** | Gesamtpreis: {CIJENA} € — vollständig über Booking.com (Payments by Booking.com) bezahlt. Keine Zahlung für den Aufenthalt beim Check-in. |
| **ES** | Precio total: {CIJENA} € — pagado íntegramente a través de Booking.com (Payments by Booking.com). No hay que pagar la estancia en el check-in. |
| **FR** | Prix total : {CIJENA} € — entièrement réglé via Booking.com (Payments by Booking.com). Aucun paiement du séjour à l’arrivée. |

### Gotovina

| Jezik | Tekst |
|-------|-------|
| **HR** | Ukupna cijena: {CIJENA} € — plaćanje gotovinom na check-inu. |
| **EN** | Total price: €{CIJENA} — payment in cash at check-in. |
| **DE** | Gesamtpreis: {CIJENA} € — Zahlung bar beim Check-in. |
| **ES** | Precio total: {CIJENA} € — pago en efectivo en el check-in. |
| **FR** | Prix total : {CIJENA} € — paiement en espèces à l’arrivée. |

### Kartica

| Jezik | Tekst |
|-------|-------|
| **HR** | Ukupna cijena: {CIJENA} € — plaćanje karticom na check-inu. |
| **EN** | Total price: €{CIJENA} — payment by card at check-in. |
| **DE** | Gesamtpreis: {CIJENA} € — Zahlung mit Karte beim Check-in. |
| **ES** | Precio total: {CIJENA} € — pago con tarjeta en el check-in. |
| **FR** | Prix total : {CIJENA} € — paiement par carte à l’arrivée. |

### Transakcijski račun (izvan Booking.com)

| Jezik | Tekst |
|-------|-------|
| **HR** | Ukupna cijena: {CIJENA} € — plaćanje transakcijskim računom (prema dogovoru). |
| **EN** | Total price: €{CIJENA} — payment by bank transfer (as agreed). |
| **DE** | Gesamtpreis: {CIJENA} € — Zahlung per Banküberweisung (wie vereinbart). |
| **ES** | Precio total: {CIJENA} € — pago por transferencia bancaria (según acuerdo). |
| **FR** | Prix total : {CIJENA} € — paiement par virement bancaire (selon accord). |

---

## Check-in sat

Standard propertyja Uzorita: **od 15:00** (`Property.check_in_time`, default 15:00).

| Jezik | Tekst |
|-------|-------|
| **HR** | Check-in: {CHECK_IN} od {CHECK_IN_SAT} |
| **EN** | Check-in: {CHECK_IN} from {CHECK_IN_SAT} |
| **DE** | Check-in: {CHECK_IN} ab {CHECK_IN_SAT} Uhr |
| **ES** | Check-in: {CHECK_IN} a partir de las {CHECK_IN_SAT} |
| **FR** | Check-in : {CHECK_IN} à partir de {CHECK_IN_SAT} |

---

## Ulaz (opis + prilog)

| Jezik | Tekst |
|-------|-------|
| **HR** | Ulaz: potražite natpis „Restaurant Uzorita” i broj **58** na bijelom zidu — kapija s vinovom lozom odmah desno od znaka. (Fotografiju ulaza šaljemo u sljedećoj poruci.) |
| **EN** | Entrance: look for the “Restaurant Uzorita” sign and house number **58** on the white wall — the gate with vines is just to the right of the sign. (We’ll send a photo of the entrance in the next message.) |
| **DE** | Eingang: Schild „Restaurant Uzorita” und Hausnummer **58** an der weißen Mauer — das Tor mit Weinreben rechts neben dem Schild. (Ein Foto des Eingangs folgt in der nächsten Nachricht.) |
| **ES** | Entrada: busque el cartel «Restaurant Uzorita» y el número **58** en la pared blanca — la puerta con hiedra está justo a la derecha del cartel. (Enviaremos una foto de la entrada en el siguiente mensaje.) |
| **FR** | Entrée : repérez l’enseigne « Restaurant Uzorita » et le numéro **58** sur le mur blanc — le portail avec la vigne est juste à droite de l’enseigne. (Nous enverrons une photo de l’entrée dans le message suivant.) |

---

## Parkiranje

| Jezik | Tekst |
|-------|-------|
| **HR** | Parkiranje: u cijeloj zoni parkiranje je besplatno. Možete parkirati odmah ispred objekta; ako nema mjesta, slobodno bilo gdje u neposrednoj blizini. |
| **EN** | Parking: parking is free throughout the zone. You can park right in front of the property; if there is no space, anywhere nearby is fine. |
| **DE** | Parken: In der gesamten Zone ist das Parken kostenlos. Sie können direkt vor dem Haus parken; wenn kein Platz frei ist, finden Sie problemlos einen Parkplatz in unmittelbarer Nähe. |
| **ES** | Aparcamiento: el aparcamiento es gratuito en toda la zona. Puede aparcar justo delante del alojamiento; si no hay sitio, en cualquier lugar cercano. |
| **FR** | Stationnement : le stationnement est gratuit dans toute la zone. Vous pouvez vous garer juste devant l’établissement ; s’il n’y a pas de place, n’importe où à proximité. |

---

## Dokumenti (pred-check-in)

| Jezik | Tekst |
|-------|-------|
| **HR** | Molimo prije dolaska pošaljite nam ovdje na WhatsApp fotografije dokumenata za svakog odraslog gosta ({BROJ_ODRASLIH}): putovnica (stranica s podacima) ili osobna iskaznica (prednja + stražnja strana). Bez bljeskalice, cijeli dokument u kadru. Podatke koristimo isključivo za zakonsku prijavu boravka (eVisitor). |
| **EN** | Please send us photos of ID documents here on WhatsApp before arrival — one set per adult guest ({BROJ_ODRASLIH}): passport (biodata page) or national ID card (front + back). No flash, full document in frame. We use this data only for mandatory guest registration (eVisitor). |
| **DE** | Bitte senden Sie uns vor der Anreise Fotos der Ausweisdokumente hier per WhatsApp — pro erwachsenem Gast ({BROJ_ODRASLIH}): Reisepass (Datenseite) oder Personalausweis (Vorder- und Rückseite). Ohne Blitz, ganzes Dokument im Bild. Die Daten verwenden wir ausschließlich für die gesetzliche Meldepflicht (eVisitor). |
| **ES** | Por favor, envíenos fotos de los documentos de identidad por WhatsApp antes de la llegada — un juego por cada huésped adulto ({BROJ_ODRASLIH}): pasaporte (página de datos) o DNI (anverso y reverso). Sin flash, documento completo en la imagen. Usamos estos datos únicamente para el registro legal de la estancia (eVisitor). |
| **FR** | Veuillez nous envoyer par WhatsApp, avant votre arrivée, des photos des pièces d’identité — un jeu par adulte ({BROJ_ODRASLIH}) : passeport (page d’identité) ou carte d’identité (recto et verso). Sans flash, document entier visible. Nous utilisons ces données uniquement pour l’enregistrement légal du séjour (eVisitor). |

**Napomena:** Poruka 1 **ne** traži vrijeme dolaska — to ide u poruku 2.

---

## Footer (obavezno)

```
Managed by stay.hr — https://stay.hr/
```

---

## Predložak tijela poruke (HR)

```
Bok {IME}!

Hvala na rezervaciji u Luxury Room Uzorita B&B.

Vaša rezervacija
• Broj: {BOOKING_KOD}
• Objekt: Luxury Room Uzorita B&B
• Soba: {SOBA}
• Datumi: {CHECK_IN} – {CHECK_OUT}
• {PLAĆANJE_HR}

{CHECK_IN_HR}

Adresa
Ul. bana Josipa Jelačića 58, 22000 Šibenik
https://maps.app.goo.gl/BN15CcMmmAapmjUs7

{ULAZ_HR}

{PARKIRANJE_HR}

{DOKUMENTI_HR}

Lijep pozdrav,
Toni
Luxury Room Uzorita B&B
+385 99 838 8513

Managed by stay.hr — https://stay.hr/
```

---

## Primjeri za danas (4. 6. 2026.)

| Datoteka | Gost | Jezik |
|----------|------|-------|
| [whatsapp-5036489024-wolfgang-gross-de.txt](./whatsapp-5036489024-wolfgang-gross-de.txt) | Wolfgang Gross | DE |
| [whatsapp-6925385738-florin-stanciu-ro.txt](./whatsapp-6925385738-florin-stanciu-ro.txt) | Florin Stanciu | RO |

---

## Poruka 2 — nakon primitka dokumenata

**Hospira:** Reply → Generiraj (hint `checkin ready` automatski nakon OCR apply).

API: `{"intent": "reply", "hint": "checkin ready"}`

Isti odabir jezika kao poruka 1. Templatei u [`guest_compose.py`](../../backend/apps/communications/guest_compose.py) (`CHECKIN_READY_BODY`).

| Jezik | Sadržaj |
|-------|---------|
| **HR** | Hvala na dokumentima → podaci spremljeni, brz check-in → **Javite okvirno vrijeme dolaska** |
| **EN** | Thank you for documents → details saved, quick check-in → **approximate arrival time** |
| **DE** | Vielen Dank für die Dokumente → Daten registriert, schneller Check-in → **Ankunftszeit** |
| **ES** | Gracias por los documentos → datos registrados, check-in rápido → **hora aproximada de llegada** |
| **FR** | Merci pour les documents → données enregistrées, enregistrement rapide → **heure d’arrivée approximative** |

Primjer (HR):

```
Hvala vam na poslanim dokumentima!

Vaši podaci su spremljeni — kad stignete, check-in će proći brzo i nećete gubiti vrijeme.

Javite nam, molimo, okvirno vrijeme dolaska.

Lijep pozdrav,
{PROPERTY_NAME}

Managed by stay.hr — https://stay.hr/
```

---

## Redoslijed slanja

1. **Poruka 1** — Check-in → Generiraj (template) + pošalji WhatsApp
2. Odmah zatim slika ulaza (`.imports/20260527_140049.jpg`)
3. Nakon primitka dokumenata → OCR / apply u Hospiri
4. **Poruka 2** — Reply → Generiraj (Flutter šalje `reply` + `checkin ready` u pozadini) → pošalji WhatsApp
5. eVisitor prijava (ručno, po potrebi)
