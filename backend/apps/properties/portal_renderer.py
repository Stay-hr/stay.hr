"""Unified PortalRenderer for public guest portal and settings preview (ADR 0008)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from django.conf import settings
from django.utils import timezone

from apps.communications.guest_arrival_policy import after_hours_contact_phone
from apps.communications.guest_language_constants import normalize_iso639_1
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_message_send import build_wa_me_url
from apps.communications.key_handover_compose import reservation_key_handover_labels
from apps.integrations.whatsapp.phone import normalize_phone
from apps.properties.guest_info import (
    format_wifi_block,
    guest_maps_url,
    guest_text,
    guide_from_guest_info,
    normalize_guest_info,
    property_entrance_image_path,
    property_entrance_image_rel,
    render_parking_reply_text,
    wifi_facts_from_guest_info,
)
from apps.properties.models import Property
from apps.properties.portal_placeholders import apply_placeholders, sample_placeholder_context
from apps.properties.self_service import is_self_service_active
from apps.reservations.models import GuestPortalAccess, Reservation

PORTAL_SECTION_ORDER = (
    "welcome",
    "arrival",
    "key_guide",
    "parking",
    "wifi",
    "breakfast",
    "contact",
)

_ALLOWED_GUIDE_IMAGE_PREFIXES = (
    "assets/guest-portal/",
    "assets/whatsapp/",
)


def _text_for_lang(texts: dict[str, str], lang: str) -> str:
    base = (lang or "en").split("-")[0].lower()
    if base in texts and texts[base]:
        return texts[base]
    if texts.get("en"):
        return texts["en"]
    for value in texts.values():
        if value:
            return value
    return ""


@dataclass(frozen=True)
class GuestPortalContext:
    reservation_id: int | None
    property_name: str
    language: str
    sections: tuple[str, ...]
    content: Mapping[str, Any]
    branding: Mapping[str, Any]
    self_service_active: bool = False


def guide_step_image_path(rel: str) -> Path | None:
    """Resolve a guide step image under BASE_DIR; reject path traversal."""
    cleaned = (rel or "").strip().lstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        return None
    if not cleaned.startswith(_ALLOWED_GUIDE_IMAGE_PREFIXES):
        return None
    path = Path(settings.BASE_DIR) / cleaned
    try:
        path.resolve().relative_to(Path(settings.BASE_DIR).resolve())
    except ValueError:
        return None
    if path.is_file():
        return path
    return None


def serialize_guest_portal_context(ctx: GuestPortalContext) -> dict[str, Any]:
    return {
        "reservation_id": ctx.reservation_id,
        "property_name": ctx.property_name,
        "language": ctx.language,
        "sections": list(ctx.sections),
        "content": dict(ctx.content),
        "branding": dict(ctx.branding),
        "self_service_active": ctx.self_service_active,
    }


def _resolve_language(reservation: Reservation | None, *, language: str | None) -> str:
    override = normalize_iso639_1(language) if language else None
    if override:
        return override
    if reservation is None:
        return "en"
    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.PROACTIVE,
    )
    return normalize_iso639_1(ctx.language) or "en"


def _contact_phone(property: Property) -> str:
    direct = after_hours_contact_phone(property)
    if direct:
        return direct
    contact = property.contact if isinstance(property.contact, dict) else {}
    for key in ("phone", "mobile", "reception_phone", "whatsapp"):
        val = str(contact.get(key) or "").strip()
        if val:
            return val
    return ""


def _whatsapp_url(property: Property, phone: str) -> str:
    contact = property.contact if isinstance(property.contact, dict) else {}
    wa_raw = str(contact.get("whatsapp") or contact.get("phone") or phone or "").strip()
    digits = normalize_phone(wa_raw)
    if not digits:
        return ""
    return build_wa_me_url(digits, "")


def _welcome_message(property: Property, lang: str, *, guest_name: str) -> str:
    name = (guest_name or "").strip() or "guest"
    templates = {
        "hr": f"Dobrodošli{', ' + name if name != 'guest' else ''}! Ovdje su informacije za vaš boravak u {property.name}.",
        "en": f"Welcome{', ' + name if name != 'guest' else ''}! Here is the information for your stay at {property.name}.",
        "de": f"Willkommen{', ' + name if name != 'guest' else ''}! Hier finden Sie Infos zu Ihrem Aufenthalt in {property.name}.",
        "es": f"¡Bienvenido{', ' + name if name != 'guest' else ''}! Aquí tiene la información de su estancia en {property.name}.",
        "fr": f"Bienvenue{', ' + name if name != 'guest' else ''} ! Voici les informations pour votre séjour à {property.name}.",
        "sk": f"Vitajte{', ' + name if name != 'guest' else ''}! Tu sú informácie k vášmu pobytu v {property.name}.",
        "it": f"Benvenuti{', ' + name if name != 'guest' else ''}! Ecco le informazioni per il vostro soggiorno a {property.name}.",
    }
    base = (lang or "en").split("-")[0].lower()
    return templates.get(base) or templates["en"]


def _breakfast_payload(property: Property, lang: str) -> dict[str, str] | None:
    info = normalize_guest_info(property.guest_info)
    facts = info.get("facts") or {}
    raw_breakfast = facts.get("breakfast")
    text = ""
    breakfast_hours = ""
    if isinstance(raw_breakfast, dict):
        localized = {
            k: str(v).strip()
            for k, v in raw_breakfast.items()
            if k != "hours" and str(v or "").strip()
        }
        text = _text_for_lang(localized, lang)
        breakfast_hours = str(raw_breakfast.get("hours") or "").strip()
    if not text:
        text = guest_text(property, "breakfast", lang)
    if not text:
        return None
    payload: dict[str, str] = {"text": text}
    if breakfast_hours:
        payload["hours"] = breakfast_hours
    return payload


def _arrival_image_url(*, token: str | None, property: Property) -> tuple[str, str]:
    """Return (image_url, image_rel). Token URLs for public; relative path for preview."""
    image_rel = property_entrance_image_rel(property)
    try:
        path = property_entrance_image_path(property)
        if not path.is_file():
            return "", image_rel
        version = int(path.stat().st_mtime)
    except OSError:
        return "", image_rel
    if token:
        return f"/api/g/{token}/entrance?v={version}", image_rel
    return image_rel, image_rel


def _arrival_payload(
    property: Property,
    lang: str,
    *,
    token: str | None,
) -> dict[str, str] | None:
    text = guest_text(property, "entrance", lang)
    maps_url = guest_maps_url(property)
    image_url, image_rel = _arrival_image_url(token=token, property=property)
    if not text and not maps_url and not image_url:
        return None
    payload: dict[str, str] = {}
    if text:
        payload["text"] = text
    if maps_url:
        payload["maps_url"] = maps_url
    if image_url:
        payload["image_url"] = image_url
        payload["image_rel"] = image_rel
    return payload or None


def _wifi_payload(property: Property, lang: str) -> dict[str, str] | None:
    ssid, password = wifi_facts_from_guest_info(property.guest_info)
    block = format_wifi_block(property, lang)
    if not ssid and not block:
        return None
    payload: dict[str, str] = {}
    if ssid:
        payload["ssid"] = ssid
    if password:
        payload["password"] = password
    if block:
        payload["text"] = block
    return payload


def _step_image_url(*, token: str | None, image_rel: str, index: int) -> str:
    path = guide_step_image_path(image_rel)
    if path is None:
        return ""
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    if token:
        return f"/api/g/{token}/steps/{index}?v={version}"
    return image_rel


def _key_guide_payload(
    property: Property,
    lang: str,
    *,
    token: str | None,
    on_date: date,
    key_label: str,
    room_code: str,
    placeholder_ctx: Mapping[str, str],
) -> dict[str, Any] | None:
    if not is_self_service_active(property, on_date):
        return None
    guide = guide_from_guest_info(property.guest_info)
    steps_raw = guide.get("steps") or []
    if not isinstance(steps_raw, list) or not steps_raw:
        return None

    steps: list[dict[str, str]] = []
    for index, step in enumerate(steps_raw):
        if not isinstance(step, dict):
            continue
        caption_block = step.get("caption") if isinstance(step.get("caption"), dict) else {}
        caption = _text_for_lang(caption_block, lang)
        caption = apply_placeholders(
            caption,
            {
                **placeholder_ctx,
                "key_label": key_label or placeholder_ctx.get("key_label", ""),
                "room_code": room_code or placeholder_ctx.get("room_code", ""),
            },
        )
        image_rel = str(step.get("image") or "").strip()
        image_url = ""
        if image_rel:
            image_url = _step_image_url(token=token, image_rel=image_rel, index=index)
        if not caption and not image_url:
            continue
        item: dict[str, str] = {"index": str(index)}
        if caption:
            item["caption"] = caption
        if image_url:
            item["image_url"] = image_url
            item["image_rel"] = image_rel
        steps.append(item)

    if not steps:
        return None
    payload: dict[str, Any] = {"steps": steps}
    if room_code:
        payload["room_code"] = room_code
    if key_label:
        payload["key_label"] = key_label
    return payload


def _build_portal_context(
    property: Property,
    *,
    language: str,
    reservation_id: int | None,
    guest_name: str,
    check_in: date,
    check_out: date,
    on_date: date,
    token: str | None,
    key_label: str,
    room_code: str,
    reservation_notes: str,
    placeholder_ctx: Mapping[str, str],
) -> GuestPortalContext:
    self_service_active = is_self_service_active(property, on_date)
    content: dict[str, Any] = {}
    sections: list[str] = []

    welcome = {
        "property_name": property.name,
        "guest_name": guest_name,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "message": _welcome_message(property, language, guest_name=guest_name),
    }
    content["welcome"] = welcome
    sections.append("welcome")

    arrival = _arrival_payload(property, language, token=token)
    if arrival:
        content["arrival"] = arrival
        sections.append("arrival")

    key_guide = _key_guide_payload(
        property,
        language,
        token=token,
        on_date=on_date,
        key_label=key_label,
        room_code=room_code,
        placeholder_ctx=placeholder_ctx,
    )
    if key_guide:
        content["key_guide"] = key_guide
        sections.append("key_guide")

    parking_text = render_parking_reply_text(
        property,
        language,
        variant="post_checkin",
        reservation_notes=reservation_notes,
    )
    if parking_text:
        content["parking"] = {"text": parking_text}
        sections.append("parking")

    wifi = _wifi_payload(property, language)
    if wifi:
        content["wifi"] = wifi
        sections.append("wifi")

    breakfast = _breakfast_payload(property, language)
    if breakfast:
        content["breakfast"] = breakfast
        sections.append("breakfast")

    phone = _contact_phone(property)
    wa_url = _whatsapp_url(property, phone)
    if phone or wa_url:
        contact: dict[str, str] = {}
        if phone:
            contact["phone"] = phone
        if wa_url:
            contact["whatsapp_url"] = wa_url
        content["contact"] = contact
        sections.append("contact")

    ordered = tuple(s for s in PORTAL_SECTION_ORDER if s in sections)
    branding = property.branding if isinstance(property.branding, dict) else {}

    return GuestPortalContext(
        reservation_id=reservation_id,
        property_name=property.name,
        language=language,
        sections=ordered,
        content=content,
        branding=branding,
        self_service_active=self_service_active,
    )


class PortalRenderer:
    """Sole builder of portal sections + content (public + preview)."""

    @classmethod
    def render_for_access(
        cls,
        access: GuestPortalAccess,
        *,
        language: str | None = None,
    ) -> GuestPortalContext:
        reservation = access.reservation
        prop = reservation.property
        lang = _resolve_language(reservation, language=language)
        key_label, room_code = reservation_key_handover_labels(reservation)
        guest_name = (reservation.booker_name or "").strip()
        ssid, password = wifi_facts_from_guest_info(prop.guest_info)
        placeholder_ctx = {
            "guest_name": guest_name or sample_placeholder_context()["guest_name"],
            "property_name": prop.name,
            "room_name": room_code,
            "room_code": room_code,
            "key_label": key_label,
            "checkin_date": reservation.check_in.isoformat(),
            "checkout_date": reservation.check_out.isoformat(),
            "wifi_ssid": ssid,
            "wifi_password": password,
        }
        return _build_portal_context(
            prop,
            language=lang,
            reservation_id=reservation.pk,
            guest_name=guest_name,
            check_in=reservation.check_in,
            check_out=reservation.check_out,
            on_date=reservation.check_in,
            token=str(access.token),
            key_label=key_label,
            room_code=room_code,
            reservation_notes=getattr(reservation, "notes", "") or "",
            placeholder_ctx=placeholder_ctx,
        )

    @classmethod
    def render_for_property(
        cls,
        property: Property,
        *,
        language: str | None = None,
        on_date: date | None = None,
    ) -> GuestPortalContext:
        """Property-scoped preview — same shape as public, sample placeholders."""
        lang = _resolve_language(None, language=language)
        today = on_date or timezone.localdate()
        check_out = today + timedelta(days=3)
        sample = sample_placeholder_context(property_name=property.name)
        ssid, password = wifi_facts_from_guest_info(property.guest_info)
        if ssid:
            sample["wifi_ssid"] = ssid
        if password:
            sample["wifi_password"] = password
        sample["checkin_date"] = today.isoformat()
        sample["checkout_date"] = check_out.isoformat()
        return _build_portal_context(
            property,
            language=lang,
            reservation_id=None,
            guest_name=sample["guest_name"],
            check_in=today,
            check_out=check_out,
            on_date=today,
            token=None,
            key_label=sample["key_label"],
            room_code=sample["room_code"],
            reservation_notes="",
            placeholder_ctx=sample,
        )


def entrance_image_file_for_access(access: GuestPortalAccess) -> Path | None:
    """Return absolute path to entrance image if configured and present on disk."""
    prop = access.reservation.property
    path = property_entrance_image_path(prop)
    if path.is_file():
        return path
    fallback = Path(settings.BASE_DIR) / "assets" / "whatsapp" / "uzorita_entrance.jpg"
    if fallback.is_file():
        return fallback
    return None


def key_guide_step_file_for_access(access: GuestPortalAccess, index: int) -> Path | None:
    """Return absolute path for guide step image ``index`` when portal is self-service active."""
    reservation = access.reservation
    prop = reservation.property
    if not is_self_service_active(prop, reservation.check_in):
        return None
    guide = guide_from_guest_info(prop.guest_info)
    steps = guide.get("steps") or []
    if not isinstance(steps, list) or index < 0 or index >= len(steps):
        return None
    step = steps[index]
    if not isinstance(step, dict):
        return None
    return guide_step_image_path(str(step.get("image") or ""))
