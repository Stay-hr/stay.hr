# eVisitor — Flutter (Hospira) upute

Opća arhitektura, statusi, recovery, checkout i audit opisani su u [evisitor.md](evisitor.md). Ovaj dokument pokriva Flutter/Hospira klijent i prikaz u UI-u.

## API: prijava gosta

```http
POST /api/v1/reception/reservations/{reservationId}/guests/{guestId}/evisitor-submit/
Authorization: Bearer {api_token}
Content-Type: application/json

{}
```

Opcionalno ponovni pokušaj nakon `failed`:

```json
{ "force_retry": true }
```

## Odgovori

### Uspjeh (200)

```json
{
  "status": "sent",
  "registration_id": "a01c2e9f-3839-4f0e-b39b-775e107d6f36",
  "submitted_at": "2026-05-20T15:52:00.803833Z"
}
```

### Već prijavljen — usklađeno (200, od backend recovery)

Kad eVisitor vrati „već prijavljena”, backend (od svibnja 2026) automatski spremi postojeći ID i vrati:

```json
{
  "status": "sent",
  "registration_id": "a01c2e9f-3839-4f0e-b39b-775e107d6f36",
  "submitted_at": "...",
  "recovered": true,
  "message": "Osoba Lauriane Saulnier je već prijavljena na datum 20.5.2026. ..."
}
```

U UI prikaži **informativni snackbar** (npr. zelena/info), ne crvenu grešku:

```dart
if (response.statusCode == 200) {
  final data = jsonDecode(response.body) as Map<String, dynamic>;
  final status = data['status'] as String?;
  if (status == 'sent' && data['recovered'] == true) {
    showSnackBar(context, data['message'] ?? 'Gost je već prijavljen u eVisitoru.');
    refreshGuest(); // evisitor_status -> sent
    return;
  }
  // običan uspjeh
}
```

### Već prijavljen — bez recovery (502, stariji backend ili druga greška)

```json
{
  "status": "failed",
  "user_message": "Osoba Lauriane Saulnier je već prijavljena...",
  "system_message": "..."
}
```

Parsiranje `user_message` (templated ili čitljiv tekst):

```dart
String evisitorErrorMessage(Map<String, dynamic> body) {
  final raw = (body['user_message'] ?? body['message'] ?? '').toString();
  if (raw.contains('|||')) {
    final parts = raw.split('|||');
    if (parts.length >= 5) {
      return 'Osoba ${parts[1]} ${parts[2]} je već prijavljena na datum ${parts[3]}. '
          'ID prijave: ${parts[4].replaceAll(']]]', '')}';
    }
  }
  return raw.replaceAll('[[[', '').replaceAll(']]]', '').trim();
}
```

Ako poruka sadrži **„već prijavljena”**, prikaži info dialog, ne „Submit failed”.

### Validacija (400)

```json
{
  "status": "validation_failed",
  "message": "Podaci nisu potpuni za eVisitor prijavu.\n...",
  "field_errors": { "date_of_expiry": "..." }
}
```

### Konfiguracija (400)

```json
{ "status": "config_error", "message": "..." }
```

### Gost već `sent` u bazi (200, bez poziva eVisitora)

```json
{
  "status": "sent",
  "registration_id": "...",
  "message": "Gost je već prijavljen u eVisitor."
}
```

## Prikaz statusa na gostu

Koristi `guest.evisitor_status` s liste rezervacije:

| status | UI |
|--------|-----|
| `not_sent` | Nije poslano |
| `pending` | U tijeku |
| `sent` | Prijavljeno |
| `checked_out` | Odjavljeno |
| `failed` | CheckIn nije uspio — moguće ponoviti prijavu |
| `checkout_failed` | CheckIn OK, CheckOut nije uspio — ponoviti odjavu (`evisitor-checkout`), **ne** prijavu |

Nakon uspješnog submita ili `recovered: true`, osvježi gosta s API-ja (`GET .../reservations/{id}/`).

Per-guest odjava:

```text
POST /api/v1/reception/reservations/{reservationId}/guests/{guestId}/evisitor-checkout/
```

## Preporuka: client helper

```dart
Future<EvisitorSubmitResult> submitEvisitor({
  required int reservationId,
  required int guestId,
  bool forceRetry = false,
}) async {
  final res = await api.post(
    '/reception/reservations/$reservationId/guests/$guestId/evisitor-submit/',
    body: forceRetry ? {'force_retry': true} : {},
  );
  final data = jsonDecode(res.body) as Map<String, dynamic>;

  if (res.statusCode == 200) {
    return EvisitorSubmitResult.success(
      status: data['status'] as String,
      registrationId: data['registration_id'] as String?,
      recovered: data['recovered'] == true,
      message: data['message'] as String?,
    );
  }
  if (res.statusCode == 400) {
    return EvisitorSubmitResult.validation(
      message: data['message'] as String? ?? '',
      fieldErrors: Map<String, String>.from(data['field_errors'] ?? {}),
    );
  }
  if (res.statusCode == 502) {
    return EvisitorSubmitResult.apiError(
      userMessage: evisitorErrorMessage(data),
    );
  }
  throw ApiException(res.statusCode, res.body);
}
```

## Deploy backend promjene

Recovery „već prijavljena” zahtijeva novi backend image:

```bash
docker compose build django && docker compose up -d django
```
