# Guest reply language i sastavljanje odgovora

Kako se određuje jezik automatskog odgovora gostu i kome pripada boilerplate (greeting, sign-off, naziv objekta, footer).

**Kod:** [`language_detection.py`](../../backend/apps/communications/language_detection.py), [`guest_language_policy.py`](../../backend/apps/communications/guest_language_policy.py), [`guest_language_resolver.py`](../../backend/apps/communications/guest_language_resolver.py), [`guest_reply_sanitize.py`](../../backend/apps/communications/guest_reply_sanitize.py)

---

## Prioritet jezika (REACTIVE)

```text
override → high-confidence message → LLM reply_language → message → conversation → country → tenant/property default → en
```

PROACTIVE preskače poruku: `override → country → tenant/property default → en`.

Ključno pravilo: **detekcija poruke s `confidence >= MESSAGE_OVERRIDES_LLM_THRESHOLD` (0.85) pobjeđuje jezik koji LLM prijavi za sebe.** Engleska poruka s jednom stranom riječju (`molimteh`) ne smije se odgovoriti na tom stranom jeziku. Ispod tog praga LLM claim još uvijek pobjeđuje, jer su naše heuristike slijepe za jezike bez markera.

## Detekcija

`detect()` boduje **sve** jezike i uzima pobjednika, umjesto "prvi marker pobjeđuje". Po jeziku postoje četiri vrste markera:

| Grupa | Podudaranje | Primjer |
|-------|-------------|---------|
| `CHARS` | substring u cijelom tekstu | `ß`, `ľ`, `ô` |
| `STEMS` | prefiks tokena | `dolaz` hvata `dolazimo` |
| `WORDS` | točan token | `ste`, `li`, `is`, `it` |
| `PHRASES` | substring, višerječni signal | `por favor`, `per favore` |

Engleski mora pobijediti **strogo** (kod izjednačenja pobjeđuje ne-engleski), jer su neke engleske funkcijske riječi (`to`, `a`, `i`) ujedno hrvatske/rumunjske/mađarske. Domenske riječi koje izgledaju isto u više jezika (`parking`, `hotel`, `wifi`, `camera`) nisu u engleskom setu.

### Confidence ljestvica

| Pogodci | Confidence | Efekt |
|---------|-----------|-------|
| 0 | 0.35 (`en` fallback) | ne perzistira, ne pregazi LLM |
| 1 | 0.5 | ne perzistira, ne pregazi LLM |
| 2 | 0.7 | perzistira `conversation_language`, ne pregazi LLM |
| 3-4, prednost >= 2 | 0.85 | pregazi LLM claim |
| 5+, prednost >= 2 | 0.9 | pregazi LLM claim |

Jedan signal je namjerno **ispod** `CONVERSATION_UPDATE_THRESHOLD` (0.65): izolirani `Thanks` ne smije trajno prebaciti jezik razgovora, jer bi to bilo u suprotnosti s pravilom o dominantnom jeziku.

## Sastavljanje odgovora (parking auto-reply)

LLM vraća **samo tijelo odgovora**. Wrapper je jedini vlasnik boilerplatea:

```text
greeting            <- _format_parking_reply_with_greeting
                    <- (prazan red)
tijelo              <- LLM ili render_parking_reply_text
                    <- (prazan red)
sign-off
naziv objekta
                    <- (prazan red)
Managed by stay.hr — https://stay.hr/
```

Pravila:

1. **Prompt** zabranjuje greeting, sign-off, naziv objekta i footer u `reply_text`.
2. **`strip_reply_boilerplate(text, *, property_name="")`** defenzivno uklanja ono što wrapper ionako dodaje. Naziv objekta briše se samo uz točan trailing match i samo ako je `property_name` predan. Ako nakon sanitizacije nema tijela → `GuestComposeError` → deterministički fallback.
3. **`reported_reply_language`** u `ParkingLlmResult` je sirovi (normalizirani i validirani) claim modela — **nikad ne prolazi kroz resolver**. `_handle_parking_llm` jednom razriješi `final_language` i usporedi ga s claimom; ako se razlikuju, LLM tijelo se odbacuje i koristi se `render_parking_reply_text(property, final_language)`.

Invarijanta koja štiti od regresije (incident na rezervaciji 1137):

```text
poruka: engleski, detektor en @ 0.9, LLM javlja hr, LLM tijelo hrvatsko
=> final_language = en, LLM tijelo se NE šalje, draft.language = en,
   greeting/sign-off na engleskom, "Managed by stay.hr" točno 1x
```

Test: `GuestParkingLlmInboundTests.test_english_message_with_foreign_filler_word_is_answered_in_english` u [`test_guest_parking_inbound.py`](../../backend/apps/communications/tests/test_guest_parking_inbound.py).

## Testovi

```bash
./scripts/run-tests-postgis.sh apps.communications.tests -v 2
```

Relevantni moduli: `test_language_detection` (matrica svih podržanih jezika), `test_guest_language_policy` (prioritet), `test_guest_reply_sanitize`, `test_guest_parking_inbound`.
