"""Match OCR persons to active reservations and guest slots.

Identity confidence order (terminal STOP at first hit) — ADR 0017:

1. document_number (highest)
2. MRZ
3. OCR name + DOB
4. fuzzy name
5. empty slot (lowest)
"""

from __future__ import annotations

import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.reservations.booking_xls_import import (
    _guest_display_name,
    _normalize_guest_name_key,
)
from apps.reservations.guest_slots import PLACEHOLDER_NAME, is_unfilled_guest
from apps.reservations.models import Guest, Reservation

ZAGREB = ZoneInfo("Europe/Zagreb")
ACTIVE_STATUSES = frozenset(
    {
        Reservation.Status.EXPECTED,
        Reservation.Status.CHECKED_IN,
    }
)

# Explicit priority ladder — do not reorder casually.
IDENTITY_MATCH_ORDER = (
    "document_number",
    "mrz",
    "name_dob",
    "name",
    "unfilled_slot",
)

_HARD_IDENTITY_TYPES = frozenset({"document_number", "mrz"})
_STRONG_MATCH_TYPES = frozenset({"document_number", "mrz", "name_dob", "name"})


def _person_full_name(person: dict) -> str:
    given = str(person.get("given_names") or "").strip()
    surnames = str(person.get("surnames") or "").strip()
    if given and surnames:
        return f"{given} {surnames}"
    return given or surnames


def _clean_name_token(token: str) -> str:
    return re.sub(r"[^\w'-]", "", (token or "").strip(), flags=re.UNICODE)


def _first_given_name(given: str) -> str:
    """First name token; OCR often uses comma-separated given names."""
    head = (given.split(",")[0] if "," in given else given).strip()
    return _clean_name_token((head.split() or [""])[0])


def _short_name_key(given: str, surnames: str) -> str:
    first = _first_given_name(given)
    if first and surnames:
        return f"{first} {surnames}"
    return ""


def _person_surname_tokens(person: dict) -> set[str]:
    """Surname tokens from structured fields and OCR blobs (married names, commas)."""
    tokens: set[str] = set()
    raw_surnames = str(person.get("surnames") or "").strip()
    if raw_surnames:
        for part in re.split(r"[\s,]+", raw_surnames):
            key = _normalize_guest_name_key(part)
            if key:
                tokens.add(key)

    raw_given = str(person.get("given_names") or "").strip()
    blob = _person_full_name(person)
    for source in (raw_given, blob):
        married = re.search(r"\bep\.?\s*([A-Za-zÀ-ž'-]+)", source, re.IGNORECASE)
        if married:
            key = _normalize_guest_name_key(married.group(1))
            if key:
                tokens.add(key)
        words = re.findall(r"[A-Za-zÀ-ž'-]+", source)
        if len(words) >= 2:
            key = _normalize_guest_name_key(words[-1])
            if key:
                tokens.add(key)
    return tokens


def _person_name_keys(person: dict) -> set[str]:
    keys: set[str] = set()
    raw_given = str(person.get("given_names") or "").strip()
    raw_surnames = str(person.get("surnames") or "").strip()
    full = _person_full_name(person)
    full_key = _normalize_guest_name_key(full)
    if full_key:
        keys.add(full_key)

    surnames = _normalize_guest_name_key(raw_surnames)
    given = _normalize_guest_name_key(raw_given)
    if surnames and given:
        keys.add(f"{given} {surnames}")
        short = _short_name_key(given, surnames)
        if short:
            keys.add(short)
        first_given = _first_given_name(raw_given)
        if first_given and surnames:
            keys.add(_normalize_guest_name_key(f"{first_given} {surnames}"))

    for surname_token in _person_surname_tokens(person):
        keys.add(surname_token)
        if raw_given:
            first_given = _first_given_name(raw_given)
            if first_given:
                keys.add(_normalize_guest_name_key(f"{first_given} {surname_token}"))
            for comma_part in raw_given.split(","):
                part = _clean_name_token(comma_part)
                if part:
                    keys.add(_normalize_guest_name_key(f"{part} {surname_token}"))

    if surnames:
        keys.add(surnames)
    return {k for k in keys if k}


def _booker_name_keys(reservation: Reservation) -> set[str]:
    booker = _normalize_guest_name_key(reservation.booker_name or "")
    if not booker:
        return set()
    keys = {booker}
    parts = booker.split()
    if len(parts) >= 2:
        keys.add(f"{parts[0]} {parts[-1]}")
        keys.add(parts[-1])
    return keys


def _booker_surname_tokens(reservation: Reservation) -> set[str]:
    booker = _normalize_guest_name_key(reservation.booker_name or "")
    parts = booker.split()
    if len(parts) >= 2:
        return {parts[-1]}
    return set()


def _booker_first_name_key(reservation: Reservation) -> str:
    booker = (reservation.booker_name or "").strip()
    parts = booker.split()
    if not parts:
        return ""
    return _normalize_guest_name_key(parts[0])


def _person_first_name_key(person: dict) -> str:
    return _normalize_guest_name_key(
        _first_given_name(str(person.get("given_names") or ""))
    )


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(
                    current[-1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + cost,
                )
            )
        prev = current
    return prev[-1]


def _surname_keys_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    min_len = 5
    if len(left) >= min_len and len(right) >= min_len:
        return _levenshtein(left, right) <= 1
    return False


def _name_token_sets_overlap(left_keys: set[str], right_keys: set[str]) -> bool:
    if left_keys & right_keys:
        return True
    for left in left_keys:
        for right in right_keys:
            if _surname_keys_compatible(left, right):
                return True
    return False


def _booker_person_overlap(
    reservation: Reservation,
    keys: set[str],
    person_surnames: set[str],
) -> bool:
    booker_keys = _booker_name_keys(reservation)
    booker_surnames = _booker_surname_tokens(reservation)
    if booker_keys & keys or booker_keys & person_surnames:
        return True
    if _name_token_sets_overlap(booker_surnames, person_surnames):
        return True
    if _name_token_sets_overlap(booker_keys, person_surnames):
        return True
    if _name_token_sets_overlap(booker_surnames, keys):
        return True
    return False


def _unfilled_slot_allowed_for_person(
    reservation: Reservation,
    person: dict,
    keys: set[str],
    person_surnames: set[str],
) -> bool:
    if not person_surnames:
        return True
    if _booker_person_overlap(reservation, keys, person_surnames):
        return True
    booker_first = _booker_first_name_key(reservation)
    person_first = _person_first_name_key(person)
    return bool(booker_first and person_first and booker_first == person_first)


def active_reservations_for_intake(tenant_id: int) -> list[Reservation]:
    today = timezone.now().astimezone(ZAGREB).date()
    window_start = today - timedelta(days=3)
    window_end = today + timedelta(days=14)
    qs = (
        Reservation.objects.filter(
            tenant_id=tenant_id,
            status__in=ACTIVE_STATUSES,
            check_in__lte=window_end,
            check_out__gte=window_start,
        )
        .prefetch_related("guests")
        .order_by("check_in", "id")
    )
    return list(qs)


def _guest_name_keys(guest: Guest) -> set[str]:
    keys: set[str] = set()
    for raw in (
        _guest_display_name(guest),
        f"{guest.first_name} {guest.last_name}".strip(),
        f"{guest.last_name} {guest.first_name}".strip(),
    ):
        key = _normalize_guest_name_key(raw)
        if key:
            keys.add(key)
            parts = key.split()
            if len(parts) >= 2:
                keys.add(f"{parts[0]} {parts[-1]}")
    return keys


def _guest_name_matches(guest: Guest, keys: set[str]) -> bool:
    return bool(_guest_name_keys(guest) & keys)


def _fuzzy_guest_match(
    reservation: Reservation,
    keys: set[str],
    *,
    person: dict | None = None,
    person_surnames: set[str] | None = None,
    exclude: set[int] | None = None,
) -> Guest | None:
    blocked = exclude or set()
    person_surnames = person_surnames or set()
    for guest in reservation.guests.all():
        if guest.pk in blocked:
            continue
        if _guest_name_matches(guest, keys):
            return guest

    if _booker_person_overlap(reservation, keys, person_surnames):
        booker_first = _booker_first_name_key(reservation)
        person_first = _person_first_name_key(person) if person else ""
        if booker_first and person_first and booker_first != person_first:
            return None
        for guest in reservation.guests.all():
            if guest.pk in blocked:
                continue
            if guest.is_primary and is_unfilled_guest(guest):
                return guest
        unfilled = _find_unfilled_slot(reservation, exclude=blocked)
        if unfilled is not None:
            return unfilled
        for guest in reservation.guests.all():
            if guest.pk in blocked:
                continue
            if guest.is_primary:
                return guest
    return None


def _is_placeholder_guest(guest: Guest) -> bool:
    name = (guest.name or "").strip()
    if name == PLACEHOLDER_NAME:
        return True
    first = (guest.first_name or "").strip()
    last = (guest.last_name or "").strip()
    return first == "Novi" and last == "gost"


def _find_unfilled_slot(
    reservation: Reservation,
    *,
    exclude: set[int] | None = None,
) -> Guest | None:
    """Prefer secondary placeholders over primary booker slots still missing ID data."""
    blocked = exclude or set()
    unfilled: list[Guest] = []
    for guest in reservation.guests.all():
        if guest.pk in blocked:
            continue
        if is_unfilled_guest(guest):
            unfilled.append(guest)

    if not unfilled:
        return None

    for guest in unfilled:
        if not guest.is_primary and _is_placeholder_guest(guest):
            return guest

    for guest in unfilled:
        if not guest.is_primary:
            return guest

    return unfilled[0]


def _reservations_with_name_matches(results: list[dict]) -> set[int]:
    ids: set[int] = set()
    for result in results:
        for candidate in result.get("candidates") or []:
            if candidate.get("match_type") in {"name", "name_dob", "document_number", "mrz"}:
                ids.add(int(candidate["reservation_id"]))
    return ids


def _apply_batch_reservation_heuristic(
    results: list[dict],
    *,
    reservations_by_id: dict[int, Reservation] | None = None,
) -> None:
    """When one reservation has a name match, auto-apply companions on the same reservation."""
    name_reservations = _reservations_with_name_matches(results)
    if len(name_reservations) != 1:
        return

    target_reservation_id = next(iter(name_reservations))
    reservation = None
    if reservations_by_id:
        reservation = reservations_by_id.get(target_reservation_id)
    if reservation is None:
        reservation = Reservation.objects.prefetch_related("guests").filter(pk=target_reservation_id).first()
    if reservation is None:
        return

    assigned_guest_ids = {
        int(result["guest_id"])
        for result in results
        if result.get("auto_apply") and result.get("guest_id")
    }

    for result in results:
        if result.get("auto_apply"):
            continue

        reservation_candidates = [
            candidate
            for candidate in result.get("candidates") or []
            if int(candidate["reservation_id"]) == target_reservation_id
            and int(candidate["guest_id"]) not in assigned_guest_ids
        ]
        guest = None
        if len(reservation_candidates) == 1:
            guest_id = int(reservation_candidates[0]["guest_id"])
            guest = next((g for g in reservation.guests.all() if g.pk == guest_id), None)
        if guest is None:
            guest = _find_unfilled_slot(reservation, exclude=assigned_guest_ids)
        if guest is None:
            continue

        result["auto_apply"] = True
        result["reservation_id"] = target_reservation_id
        result["guest_id"] = guest.pk
        result["guest_name"] = _guest_display_name(guest)
        result["reservation_label"] = _reservation_label(reservation)
        assigned_guest_ids.add(guest.pk)




def _person_dob(person: dict):
    raw = str(person.get("date_of_birth") or "").strip()
    if not raw:
        return None
    return parse_date(raw)


def _guest_by_name_and_dob(
    reservation: Reservation,
    keys: set[str],
    person: dict,
    *,
    exclude: set[int] | None = None,
) -> Guest | None:
    """Priority 3: name keys + matching date_of_birth when both sides have DOB."""
    person_dob = _person_dob(person)
    if person_dob is None or not keys:
        return None
    blocked = exclude or set()
    for guest in reservation.guests.all():
        if guest.pk in blocked:
            continue
        if guest.date_of_birth is None:
            continue
        if guest.date_of_birth != person_dob:
            continue
        if _guest_name_matches(guest, keys):
            return guest
    return None


def _match_guest_for_reservation(
    reservation: Reservation,
    person: dict,
    *,
    keys: set[str],
    person_surnames: set[str],
    exclude: set[int],
    scoped_single_reservation: bool,
) -> tuple[Guest | None, str]:
    """Apply IDENTITY_MATCH_ORDER; hard identity is terminal (no lower steps)."""
    from apps.reservations.document_intake_identity import (
        find_guest_by_identity,
        record_hard_match_metric,
    )

    guest, match_type = find_guest_by_identity(
        reservation, person, exclude=exclude
    )
    if guest is not None:
        record_hard_match_metric(match_type, reservation_id=reservation.pk)
        return guest, match_type

    guest = _guest_by_name_and_dob(
        reservation, keys, person, exclude=exclude
    )
    if guest is not None:
        return guest, "name_dob"

    if keys:
        guest = _fuzzy_guest_match(
            reservation,
            keys,
            person=person,
            person_surnames=person_surnames,
            exclude=exclude,
        )
        if guest is not None:
            return guest, "name"

    guest = _find_unfilled_slot(reservation, exclude=exclude)
    if guest is not None:
        if (
            not scoped_single_reservation
            and not _unfilled_slot_allowed_for_person(
                reservation,
                person,
                keys,
                person_surnames,
            )
        ):
            return None, ""
        return guest, "unfilled_slot"

    return None, ""


def match_persons_to_guests(
    *,
    tenant_id: int,
    persons: list[dict],
    reservation_id: int | None = None,
) -> list[dict]:
    """Internal matching primitive. Production code must use ``run_document_intake_matching_pipeline()`` instead."""
    if reservation_id:
        reservations = list(
            Reservation.objects.filter(
                pk=reservation_id,
                tenant_id=tenant_id,
                status__in=ACTIVE_STATUSES,
            ).prefetch_related("guests")
        )
    else:
        reservations = active_reservations_for_intake(tenant_id)
    scoped_single_reservation = len(reservations) == 1
    results: list[dict] = []
    assigned_guest_ids: set[int] = set()

    for idx, person in enumerate(persons):
        keys = _person_name_keys(person)
        person_surnames = _person_surname_tokens(person)
        full_name = _person_full_name(person)
        candidates: list[dict] = []

        for reservation in reservations:
            guest, match_type = _match_guest_for_reservation(
                reservation,
                person,
                keys=keys,
                person_surnames=person_surnames,
                exclude=assigned_guest_ids,
                scoped_single_reservation=scoped_single_reservation,
            )
            if guest is None:
                continue

            candidates.append(
                {
                    "reservation_id": reservation.pk,
                    "guest_id": guest.pk,
                    "guest_name": _guest_display_name(guest),
                    "reservation_label": _reservation_label(reservation),
                    "match_type": match_type,
                    "check_in_date": reservation.check_in.isoformat(),
                }
            )

        strong_matches = [
            c for c in candidates if c.get("match_type") in _STRONG_MATCH_TYPES
        ]
        if strong_matches:
            # Prefer identity/name over empty slots on other reservations.
            candidates = strong_matches

        confidence = _confidence_for_candidates(candidates, keys)
        if len(strong_matches) == 1:
            best = strong_matches[0]
            auto_apply = True
        elif len(candidates) == 1:
            best = candidates[0]
            auto_apply = True
        else:
            best = None
            auto_apply = False

        if best and auto_apply:
            assigned_guest_ids.add(int(best["guest_id"]))

        results.append(
            {
                "person_index": idx,
                "person_name": full_name,
                "confidence": confidence,
                "auto_apply": auto_apply,
                "candidates": candidates,
                "reservation_id": best["reservation_id"] if best else None,
                "guest_id": best["guest_id"] if best else None,
                "guest_name": best["guest_name"] if best else "",
                "reservation_label": best["reservation_label"] if best else "",
                "match_type": best.get("match_type") if best else "",
            }
        )

    _apply_batch_reservation_heuristic(results)
    return results


def enforce_unique_guest_assignments(matches: list[dict]) -> list[dict]:
    """Sole 1:1 gate: first claim per guest_id wins (person_index order)."""
    ordered = sorted(matches, key=lambda item: int(item.get("person_index", 0)))
    seen_guest_ids: set[int] = set()
    deduped: list[dict] = []

    for match in ordered:
        copy = dict(match)
        guest_id = copy.get("guest_id")
        if copy.get("auto_apply") and guest_id:
            gid = int(guest_id)
            if gid in seen_guest_ids:
                copy["auto_apply"] = False
                copy["reject_reason"] = "duplicate_guest_assignment"
            else:
                seen_guest_ids.add(gid)
        deduped.append(copy)

    deduped.sort(key=lambda item: int(item.get("person_index", 0)))
    return deduped


def _reservation_label(reservation: Reservation) -> str:
    booker = (reservation.booker_name or "").strip()
    unit = ""
    try:
        ru = reservation.reservation_units.select_related("unit").first()
        if ru and ru.unit:
            unit = ru.unit.name or ru.unit.code or ""
    except Exception:
        pass
    parts = [f"#{reservation.pk}"]
    if booker:
        parts.append(booker)
    if unit:
        parts.append(unit)
    parts.append(reservation.check_in.isoformat())
    return " · ".join(parts)


def _confidence_for_candidates(candidates: list[dict], keys: set[str]) -> str:
    if not candidates:
        return "none"
    if len(candidates) == 1 and candidates[0].get("match_type") in _STRONG_MATCH_TYPES:
        return "high"
    if len(candidates) == 1:
        return "medium"
    strong = [c for c in candidates if c.get("match_type") in _STRONG_MATCH_TYPES]
    if len(strong) == 1:
        return "high"
    return "low"


def normalize_mrz_lines(person: dict) -> str:
    lines = person.get("mrz_lines") or []
    if isinstance(lines, list):
        cleaned = [re.sub(r"\s+", "", str(line).upper()) for line in lines if str(line).strip()]
        return "\n".join(cleaned)
    return str(lines or "").strip()
