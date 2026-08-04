from __future__ import annotations

import uuid
from datetime import date

from django.utils import timezone

from apps.integrations.evisitor.config import EvisitorRuntimeConfig
from apps.integrations.evisitor.exceptions import EvisitorValidationError
from apps.integrations.evisitor.lookups import iso2_to_iso3, map_document_type_code
from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.models import EvisitorGuestStatus, Guest, Reservation


def _format_yyyymmdd(value: date | None) -> str:
    if not value:
        return ""
    return value.strftime("%Y%m%d")


def _map_gender(sex: str) -> str:
    raw = (sex or "").strip().lower()
    if raw in {"m", "male", "muški", "muski", "muskarac", "muškarac"}:
        return "muški"
    if raw in {"f", "female", "ženski", "zenski", "zena", "žena"}:
        return "ženski"
    return ""


def build_check_in_payload(
    guest: Guest,
    *,
    config: EvisitorRuntimeConfig,
    registration_id: uuid.UUID | None = None,
    time_stay_from: str | None = None,
) -> dict:
    reservation: Reservation = guest.reservation
    errors: dict[str, str] = {}

    first_name = (guest.first_name or "").strip()
    last_name = (guest.last_name or "").strip()
    if not first_name:
        errors["first_name"] = "Ime je obavezno."
    if not last_name:
        errors["last_name"] = "Prezime je obavezno."

    gender = _map_gender(guest.sex)
    if not gender:
        errors["sex"] = (
            "Spol je obavezan (muški/ženski). "
            "Njemačka osobna često nema spol na dokumentu — unesite ručno u Hospiri."
        )

    if not guest.date_of_birth:
        errors["date_of_birth"] = "Datum rođenja je obavezan."

    property_id = reservation.property_id
    citizenship = (
        iso2_to_iso3(guest.nationality, config=config, property_id=property_id)
        or iso2_to_iso3(guest.document_country_iso2, config=config, property_id=property_id)
        or (guest.document_country_iso3 or "").strip().upper()[:3]
    )
    if not citizenship or len(citizenship) != 3:
        errors["nationality"] = "Državljanstvo (ISO3) nije poznato."

    document_type = map_document_type_code(guest.document_type, guest.document_code)
    if not document_type:
        errors["document_type"] = "Tip dokumenta nije mapiran na eVisitor šifru."

    document_number = (guest.document_number or "").strip()
    if not document_number:
        errors["document_number"] = "Broj dokumenta je obavezan."

    facility = (config.facility_code or "").strip()
    if not facility:
        errors["facility"] = "Šifra objekta (Facility) nije konfigurirana."

    if not reservation.check_in or not reservation.check_out:
        errors["stay_dates"] = "Datumi boravka rezervacije nisu postavljeni."

    country_of_residence = citizenship
    if guest.document_country_iso3:
        country_of_residence = guest.document_country_iso3.strip().upper()[:3]

    address_result = validate_evisitor_residence_address(guest.address or "")
    if not address_result.valid:
        errors["address"] = (
            address_result.errors[0]
            if address_result.errors
            else "Nije moguće odrediti grad prebivališta (CityOfResidence)."
        )

    if errors:
        raise EvisitorValidationError(
            "Podaci gosta nisu potpuni za eVisitor prijavu.",
            field_errors=errors,
        )

    reg_id = registration_id or uuid.uuid4()
    return {
        "ID": str(reg_id),
        "Facility": facility,
        "TouristName": first_name,
        "TouristSurname": last_name,
        "TouristMiddleName": "",
        "Gender": gender,
        "DateOfBirth": _format_yyyymmdd(guest.date_of_birth),
        "Citizenship": citizenship,
        "CountryOfBirth": citizenship,
        "CityOfBirth": address_result.city,
        "CountryOfResidence": country_of_residence,
        "CityOfResidence": address_result.city,
        "ResidenceAddress": address_result.normalized_address.strip()[:128],
        "DocumentType": document_type,
        "DocumentNumber": document_number[:16],
        "StayFrom": _format_yyyymmdd(reservation.check_in),
        "TimeStayFrom": (time_stay_from or "").strip() or config.default_stay_time_from,
        "ForeseenStayUntil": _format_yyyymmdd(reservation.check_out),
        "TimeEstimatedStayUntil": config.default_stay_time_until,
        "ArrivalOrganisation": config.default_arrival_organisation,
        "OfferedServiceType": config.default_offered_service_type,
        "TTPaymentCategory": config.default_payment_category,
        "TouristEmail": (guest.email or "").strip(),
        "TouristTelephone": "",
    }


def build_check_out_payload(
    guest: Guest,
    *,
    config: EvisitorRuntimeConfig,
    checkout_date: date | None = None,
) -> dict:
    reservation: Reservation = guest.reservation
    errors: dict[str, str] = {}

    registration_id = guest.evisitor_registration_id
    if not registration_id:
        errors["evisitor_registration_id"] = "Nedostaje ID eVisitor prijave."

    status = (guest.evisitor_status or "").strip()
    if status == EvisitorGuestStatus.CHECKED_OUT:
        errors["evisitor_status"] = "Gost je već odjavljen u eVisitoru."
    elif status not in (
        EvisitorGuestStatus.SENT,
        EvisitorGuestStatus.CHECKOUT_FAILED,
    ):
        errors["evisitor_status"] = "Gost nije prijavljen u eVisitoru."

    today = timezone.localdate()
    check_in = reservation.check_in
    check_out = reservation.check_out

    if checkout_date is not None:
        co_date = checkout_date
    elif check_out and check_out <= today:
        co_date = check_out
    else:
        co_date = today

    if co_date > today:
        co_date = today
    if check_in and co_date < check_in:
        if check_in <= today:
            co_date = check_in
        else:
            errors["checkout_date"] = "Datum odjave ne može biti prije datuma dolaska."

    if errors:
        raise EvisitorValidationError(
            "Podaci nisu potpuni za eVisitor odjavu.",
            field_errors=errors,
        )

    return {
        "ID": str(registration_id),
        "CheckOutDate": _format_yyyymmdd(co_date),
        "CheckOutTime": config.default_stay_time_until,
    }


def mask_payload_for_log(payload: dict) -> dict:
    masked = dict(payload)
    for key in ("DocumentNumber", "TouristEmail", "TouristTelephone", "ResidenceAddress"):
        if key in masked and masked[key]:
            masked[key] = "***"
    return masked
