from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apps.integrations.evisitor.eligibility import guest_requires_evisitor
from apps.reservations.models import Guest, IdDocument, Reservation

SideKind = Literal["front", "back"]


@dataclass(frozen=True)
class MissingIdSide:
    guest_id: int
    guest_name: str
    side: SideKind
    is_passport: bool = False


def _has_photo(field) -> bool:
    return bool(field and getattr(field, "name", ""))


def _guest_display_name(guest: Guest) -> str:
    name = (guest.name or f"{guest.first_name} {guest.last_name}".strip()).strip()
    return name or f"Guest #{guest.pk}"


def _is_passport(*, guest: Guest, id_doc: IdDocument) -> bool:
    doc_type = (guest.document_type or "").strip().lower()
    if "putovn" in doc_type or doc_type == "passport":
        return True
    if getattr(id_doc, "_passport_photo", False):
        return True
    payload = id_doc.extracted_payload if isinstance(id_doc.extracted_payload, dict) else {}
    person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
    if str(person.get("document_type") or "").lower() == "passport":
        return True
    return False


def _guest_treat_as_passport(*, guest: Guest, id_docs: list[IdDocument]) -> bool:
    doc_type = (guest.document_type or "").strip().lower()
    if "putovn" in doc_type or doc_type == "passport":
        return True
    if id_docs and all(_is_passport(guest=guest, id_doc=id_doc) for id_doc in id_docs):
        return True
    return False


def _missing_sides_for_guest(*, guest: Guest, id_docs: list[IdDocument]) -> list[MissingIdSide]:
    """Aggregate front/back across all IdDocument rows (WhatsApp may apply each side separately)."""
    name = _guest_display_name(guest)
    is_passport = _guest_treat_as_passport(guest=guest, id_docs=id_docs)
    has_front = any(_has_photo(id_doc.front_photo) for id_doc in id_docs)
    has_back = any(_has_photo(id_doc.back_photo) for id_doc in id_docs)

    if is_passport:
        if not has_front:
            return [MissingIdSide(guest.pk, name, "front", is_passport=True)]
        return []

    gaps: list[MissingIdSide] = []
    if not has_front:
        gaps.append(MissingIdSide(guest.pk, name, "front", is_passport=False))
    elif not has_back:
        gaps.append(MissingIdSide(guest.pk, name, "back", is_passport=False))
    return gaps


def find_id_document_for_side_merge(
    *,
    guest: Guest,
    tip: str,
    applying_front: bool,
    applying_back: bool,
) -> IdDocument | None:
    """Find an existing partial national ID row to attach the complementary side to."""
    if tip == "passport" or (applying_front and applying_back) or (not applying_front and not applying_back):
        return None

    for id_doc in guest.id_documents.order_by("-created_at", "-id"):
        if _is_passport(guest=guest, id_doc=id_doc):
            continue
        has_front = _has_photo(id_doc.front_photo)
        has_back = _has_photo(id_doc.back_photo)
        if applying_back and has_front and not has_back:
            return id_doc
        if applying_front and has_back and not has_front:
            return id_doc
    return None


def find_missing_id_sides(reservation: Reservation) -> list[MissingIdSide]:
    """Guests missing required front/back photos across all stored IdDocument rows."""
    reference_date = reservation.check_in
    gaps: list[MissingIdSide] = []

    guests = Guest.objects.filter(reservation=reservation).prefetch_related("id_documents")
    for guest in guests:
        if not guest_requires_evisitor(guest, reference_date=reference_date):
            continue
        id_docs = list(guest.id_documents.all())
        if not id_docs:
            continue
        gaps.extend(_missing_sides_for_guest(guest=guest, id_docs=id_docs))

    return gaps
