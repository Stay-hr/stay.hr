"""Property.guest_info schema, normalization, and text resolution for guest messages.

Optional top-level key ``canonical_language`` (ISO 639-1) selects the base language for
guest templates when the guest's reply language has no localized guest_info text.
Falls back to ``property.language``, then tenant default, then ``en``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, TypedDict

from django.conf import settings

from apps.communications.guest_compose_defaults import (
    DEFAULT_ENTRANCE_IMAGE,
    DEFAULT_TEXTS,
    MAPS_LINK,
)
from apps.communications.guest_language_constants import TEMPLATE_LANGS
from apps.properties.models import Property

GUEST_INFO_TEXT_KEYS = frozenset(DEFAULT_TEXTS.keys())

GUIDE_SECTION_KEYS = frozenset(
    {
        "intro",
        "entrance",
        "cabinet",
        "key",
        "stairs",
        "breakfast",
        "parking",
        "documents",
        "checkout",
        "contact",
    }
)

DEFAULT_GUIDE_SECTION_ORDER = [
    "intro",
    "entrance",
    "cabinet",
    "key",
    "stairs",
    "breakfast",
    "parking",
    "documents",
    "checkout",
    "contact",
]

LOCALIZED_FACT_KEYS = frozenset(
    {
        "key_handover",
        "late_arrival",
        "breakfast",
        "tourist_tax",
    }
)

WIFI_LABELS: dict[str, dict[str, str]] = {
    "hr": {"ssid": "WiFi", "password": "Lozinka"},
    "en": {"ssid": "WiFi", "password": "Password"},
    "de": {"ssid": "WLAN", "password": "Passwort"},
    "es": {"ssid": "WiFi", "password": "Contraseña"},
    "fr": {"ssid": "WiFi", "password": "Mot de passe"},
    "sk": {"ssid": "WiFi", "password": "Heslo"},
}


class LocalizedText(TypedDict, total=False):
    hr: str
    en: str
    de: str
    es: str
    fr: str
    sk: str


class GuestInfoWifi(TypedDict, total=False):
    ssid: str
    password: str
    instructions: LocalizedText


def _normalize_localized(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        lang: str(raw.get(lang) or "").strip()
        for lang in TEMPLATE_LANGS
        if str(raw.get(lang) or "").strip()
    }


def _normalize_guide_localized(raw: Any) -> dict[str, str]:
    """Keep any ISO-ish language key (guide extras include it/nl/uk beyond TEMPLATE_LANGS)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        lang = str(key or "").strip().lower().split("-")[0]
        if len(lang) != 2 or not lang.isalpha():
            continue
        text = str(value or "").strip()
        if text:
            out[lang] = text
    return out


def _normalize_guide_steps(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    steps: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        section = str(item.get("section") or "").strip()
        if section and section not in GUIDE_SECTION_KEYS:
            continue
        image = str(item.get("image") or "").strip()
        caption = _normalize_guide_localized(item.get("caption"))
        step: dict[str, Any] = {}
        if section:
            step["section"] = section
        if image:
            step["image"] = image
        if caption:
            step["caption"] = caption
        if step:
            steps.append(step)
    return steps


def _normalize_guide(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    sections_raw = raw.get("sections") if isinstance(raw.get("sections"), dict) else {}
    normalized_sections: dict[str, dict[str, str]] = {}
    for key, block in sections_raw.items():
        section_key = str(key or "").strip()
        if section_key not in GUIDE_SECTION_KEYS:
            continue
        if isinstance(block, dict):
            localized = _normalize_guide_localized(block)
            if localized:
                normalized_sections[section_key] = localized

    order_raw = raw.get("order")
    if isinstance(order_raw, list):
        order = [
            str(key).strip()
            for key in order_raw
            if str(key).strip() in normalized_sections
        ]
    else:
        order = [key for key in DEFAULT_GUIDE_SECTION_ORDER if key in normalized_sections]

    enabled_raw = raw.get("enabled") if isinstance(raw.get("enabled"), dict) else {}
    enabled: dict[str, bool] = {}
    for key in order:
        if key in enabled_raw:
            enabled[key] = bool(enabled_raw[key])
        else:
            enabled[key] = True

    steps = _normalize_guide_steps(raw.get("steps"))
    guide: dict[str, Any] = {}
    if normalized_sections:
        guide["sections"] = normalized_sections
    if order:
        guide["order"] = order
    if enabled:
        guide["enabled"] = enabled
    if steps:
        guide["steps"] = steps
    return guide


def _normalize_breakfast_fact(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    localized = _normalize_localized(raw)
    hours = str(raw.get("hours") or "").strip()
    breakfast: dict[str, Any] = dict(localized)
    if hours:
        breakfast["hours"] = hours
    return breakfast


ParkingVariant = Literal["standard", "post_checkin"]

_PARKING_PREFIX_REQUESTED: dict[str, str] = {
    "hr": "Vidimo da ste pri rezervaciji naveli parking — ",
    "en": "We see you requested parking — ",
    "de": "Wir sehen, dass Sie Parking angefragt haben — ",
    "es": "Vemos que solicitó aparcamiento en la reserva — ",
    "fr": "Nous voyons que vous avez demandé un parking lors de la réservation — ",
    "sk": "Vidíme, že ste pri rezervácii uviedli parkovanie — ",
}


def _parse_price(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def normalize_parking_facts(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    custom = _normalize_localized(raw.get("custom"))
    price = _parse_price(raw.get("price_per_day"))
    currency = str(raw.get("currency") or "EUR").strip().upper()[:3] or "EUR"
    zone_label = str(raw.get("zone_label") or "").strip()
    price_notes = str(raw.get("price_notes") or "").strip()

    parking: dict[str, Any] = {}
    if raw.get("has_private") is not None:
        parking["has_private"] = bool(raw.get("has_private"))
    if zone_label:
        parking["zone_label"] = zone_label
    if price is not None:
        parking["price_per_day"] = str(price)
        parking["currency"] = currency
    elif raw.get("currency"):
        parking["currency"] = currency
    if price_notes:
        parking["price_notes"] = price_notes
    if raw.get("reservation_required") is not None:
        parking["reservation_required"] = bool(raw.get("reservation_required"))
    if raw.get("ev_charging") is not None:
        parking["ev_charging"] = bool(raw.get("ev_charging"))
    if raw.get("large_vehicles_allowed") is not None:
        parking["large_vehicles_allowed"] = bool(raw.get("large_vehicles_allowed"))
    if custom:
        parking["custom"] = custom
    return parking


def _normalize_wifi(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    instructions = _normalize_localized(raw.get("instructions"))
    wifi: dict[str, Any] = {}
    ssid = str(raw.get("ssid") or "").strip()
    password = str(raw.get("password") or "").strip()
    if ssid:
        wifi["ssid"] = ssid
    if password:
        wifi["password"] = password
    if instructions:
        wifi["instructions"] = instructions
    return wifi


def normalize_guest_info(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    links = data.get("links") if isinstance(data.get("links"), dict) else {}
    assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
    facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    texts = data.get("texts") if isinstance(data.get("texts"), dict) else {}
    guide = _normalize_guide(data.get("guide"))

    normalized_texts: dict[str, dict[str, str]] = {}
    for key in GUEST_INFO_TEXT_KEYS:
        block = texts.get(key)
        if isinstance(block, dict):
            localized = _normalize_localized(block)
            if localized:
                normalized_texts[key] = localized

    normalized_facts: dict[str, Any] = {
        "ai_notes": str(facts.get("ai_notes") or "").strip(),
        "reception_hours": str(facts.get("reception_hours") or "").strip(),
        "wifi": _normalize_wifi(facts.get("wifi")),
    }
    for fact_key in LOCALIZED_FACT_KEYS:
        if fact_key == "breakfast":
            breakfast = _normalize_breakfast_fact(facts.get("breakfast"))
            if breakfast:
                normalized_facts["breakfast"] = breakfast
            continue
        localized = _normalize_localized(facts.get(fact_key))
        if localized:
            normalized_facts[fact_key] = localized

    parking_facts = normalize_parking_facts(facts.get("parking"))
    if parking_facts:
        normalized_facts["parking"] = parking_facts

    result: dict[str, Any] = {
        "links": {
            "maps_url": str(links.get("maps_url") or "").strip(),
        },
        "assets": {
            "entrance_image": str(assets.get("entrance_image") or "").strip(),
        },
        "facts": normalized_facts,
        "texts": normalized_texts,
    }
    if guide:
        result["guide"] = guide
    # API/DTO schema version (ADR 0008); optional on storage.
    schema_version = data.get("schema_version")
    if schema_version is not None:
        try:
            result["schema_version"] = int(schema_version)
        except (TypeError, ValueError):
            pass
    canonical = str(data.get("canonical_language") or "").strip().lower().split("-")[0]
    if canonical:
        result["canonical_language"] = canonical
    return result


def guide_from_guest_info(raw: Any) -> dict[str, Any]:
    info = normalize_guest_info(raw)
    guide = info.get("guide")
    return guide if isinstance(guide, dict) else {}


def guide_section_text(property: Property, section_key: str, lang: str) -> str:
    guide = guide_from_guest_info(property.guest_info)
    sections = guide.get("sections") or {}
    block = sections.get(section_key)
    if isinstance(block, dict):
        return _text_for_lang(block, lang)
    return ""


def effective_unit_key_label(unit) -> str:
    """Return key-tag label for a unit (explicit key_label if present, else code heuristic)."""
    if unit is None:
        return ""
    explicit = str(getattr(unit, "key_label", "") or "").strip()
    if explicit:
        return explicit
    code = str(getattr(unit, "code", "") or "").strip()
    if len(code) >= 2 and code[0].isalpha() and code[1:].isdigit():
        return code[1:]
    return code


def breakfast_hours_from_guest_info(raw: Any, lang: str = "en") -> str:
    info = normalize_guest_info(raw)
    breakfast = (info.get("facts") or {}).get("breakfast")
    if isinstance(breakfast, dict):
        hours = str(breakfast.get("hours") or "").strip()
        if hours:
            return hours
    del lang
    return ""


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


def guest_text(
    property: Property,
    key: str,
    lang: str,
    **fmt: Any,
) -> str:
    info = normalize_guest_info(property.guest_info)
    property_block = (info.get("texts") or {}).get(key)
    if isinstance(property_block, dict) and property_block:
        template = _text_for_lang(property_block, lang)
    else:
        fallback = DEFAULT_TEXTS.get(key)
        if not fallback:
            return ""
        template = _text_for_lang(fallback, lang)
    if fmt:
        return template.format(**fmt)
    return template


def wifi_facts_from_guest_info(raw: Any) -> tuple[str, str]:
    """Return (ssid, password) from guest_info JSON."""
    info = normalize_guest_info(raw)
    wifi = (info.get("facts") or {}).get("wifi") or {}
    if not isinstance(wifi, dict):
        return "", ""
    return str(wifi.get("ssid") or "").strip(), str(wifi.get("password") or "").strip()


def parking_facts_from_guest_info(raw: Any) -> dict[str, Any]:
    info = normalize_guest_info(raw)
    facts = (info.get("facts") or {}).get("parking") or {}
    return facts if isinstance(facts, dict) else {}


def merge_parking_into_guest_info(
    raw: Any,
    *,
    has_private: bool = False,
    zone_label: str = "",
    price_per_day: str | Decimal | None = None,
    currency: str = "EUR",
    price_notes: str = "",
    reservation_required: bool = False,
    ev_charging: bool = False,
    large_vehicles_allowed: bool = True,
    custom_hr: str = "",
    custom_en: str = "",
) -> dict[str, Any]:
    info = normalize_guest_info(raw)
    facts = dict(info.get("facts") or {})
    parking: dict[str, Any] = {
        "has_private": has_private,
        "reservation_required": reservation_required,
        "ev_charging": ev_charging,
        "large_vehicles_allowed": large_vehicles_allowed,
    }
    zone_clean = (zone_label or "").strip()
    if zone_clean:
        parking["zone_label"] = zone_clean
    price = _parse_price(price_per_day)
    if price is not None:
        parking["price_per_day"] = str(price)
        parking["currency"] = (currency or "EUR").strip().upper()[:3] or "EUR"
    notes_clean = (price_notes or "").strip()
    if notes_clean:
        parking["price_notes"] = notes_clean
    custom: dict[str, str] = {}
    if (custom_hr or "").strip():
        custom["hr"] = custom_hr.strip()
    if (custom_en or "").strip():
        custom["en"] = custom_en.strip()
    if custom:
        parking["custom"] = custom
    facts["parking"] = normalize_parking_facts(parking)
    info["facts"] = facts
    return normalize_guest_info(info)


def _parking_price_phrase(parking: dict[str, Any], lang: str) -> str:
    price = _parse_price(parking.get("price_per_day"))
    currency = str(parking.get("currency") or "EUR").strip().upper()
    price_notes = str(parking.get("price_notes") or "").strip()
    if price is None:
        return price_notes
    free_phrases = {
        "hr": "parkiranje je besplatno",
        "en": "parking is free",
        "de": "Parken ist kostenlos",
        "es": "el aparcamiento es gratuito",
        "fr": "le stationnement est gratuit",
        "sk": "parkovanie je bezplatné",
    }
    priced_phrases = {
        "hr": f"cijena parkiranja je {price} {currency} po danu",
        "en": f"parking costs {price} {currency} per day",
        "de": f"Parken kostet {price} {currency} pro Tag",
        "es": f"el aparcamiento cuesta {price} {currency} por día",
        "fr": f"le stationnement coûte {price} {currency} par jour",
        "sk": f"parkovanie je {price} {currency} za deň",
    }
    base = (lang or "en").split("-")[0].lower()
    if price == 0:
        phrase = free_phrases.get(base) or free_phrases["en"]
    else:
        phrase = priced_phrases.get(base) or priced_phrases["en"]
    if price_notes:
        return f"{phrase}. {price_notes}"
    return phrase


def _generate_parking_body_from_facts(
    parking: dict[str, Any],
    lang: str,
    *,
    variant: ParkingVariant,
) -> str:
    base = (lang or "en").split("-")[0].lower()
    parts: list[str] = []

    if variant == "post_checkin":
        intros = {
            "hr": "Za parkiranje:",
            "en": "For parking:",
            "de": "Fürs Parken:",
            "es": "Para aparcar:",
            "fr": "Pour le stationnement:",
            "sk": "Pre parkovanie:",
        }
        parts.append(intros.get(base) or intros["en"])
    else:
        labels = {
            "hr": "Parkiranje:",
            "en": "Parking:",
            "de": "Parken:",
            "es": "Aparcamiento:",
            "fr": "Stationnement:",
            "sk": "Parkovanie:",
        }
        parts.append(labels.get(base) or labels["en"])

    if parking.get("has_private"):
        private_phrases = {
            "hr": "u sklopu objekta dostupan je privatni parking",
            "en": "private on-site parking is available",
            "de": "privater Parkplatz am Objekt ist verfügbar",
            "es": "hay aparcamiento privado en el alojamiento",
            "fr": "un parking privé sur place est disponible",
            "sk": "k dispozícii je súkromné parkovanie v objekte",
        }
        parts.append(private_phrases.get(base) or private_phrases["en"])

    zone_label = str(parking.get("zone_label") or "").strip()
    if zone_label:
        zone_phrases = {
            "hr": f"zona parkiranja: {zone_label}",
            "en": f"parking zone: {zone_label}",
            "de": f"Parkzone: {zone_label}",
            "es": f"zona de aparcamiento: {zone_label}",
            "fr": f"zone de stationnement : {zone_label}",
            "sk": f"zóna parkovania: {zone_label}",
        }
        parts.append(zone_phrases.get(base) or zone_phrases["en"])

    price_phrase = _parking_price_phrase(parking, base)
    if price_phrase:
        parts.append(price_phrase)

    if parking.get("reservation_required"):
        res_phrases = {
            "hr": "rezervacija parkinga je potrebna",
            "en": "parking reservation is required",
            "de": "Parkplatzreservierung ist erforderlich",
            "es": "se requiere reserva de aparcamiento",
            "fr": "la réservation du parking est requise",
            "sk": "je potrebná rezervácia parkovania",
        }
        parts.append(res_phrases.get(base) or res_phrases["en"])

    if parking.get("ev_charging"):
        ev_phrases = {
            "hr": "dostupno punjenje za električna vozila",
            "en": "EV charging is available",
            "de": "E-Ladestation verfügbar",
            "es": "carga para vehículos eléctricos disponible",
            "fr": "recharge pour véhicules électriques disponible",
            "sk": "k dispozícii nabíjanie pre elektromobily",
        }
        parts.append(ev_phrases.get(base) or ev_phrases["en"])

    if parking.get("large_vehicles_allowed") is False:
        large_phrases = {
            "hr": "velika vozila / kombiji nisu dozvoljeni",
            "en": "large vehicles / vans are not allowed",
            "de": "große Fahrzeuge / Transporter sind nicht erlaubt",
            "es": "vehículos grandes / furgonetas no están permitidos",
            "fr": "grands véhicules / fourgons non admis",
            "sk": "veľké vozidlá / dodávky nie sú povolené",
        }
        parts.append(large_phrases.get(base) or large_phrases["en"])

    custom = parking.get("custom")
    if isinstance(custom, dict):
        custom_text = _text_for_lang(custom, base)
        if custom_text:
            parts.append(custom_text)

    if len(parts) <= 1:
        return ""
    intro = parts[0]
    detail = ". ".join(parts[1:])
    return f"{intro} {detail}."


def render_parking_reply_text(
    property: Property,
    lang: str,
    *,
    variant: ParkingVariant = "standard",
    reservation_notes: str = "",
) -> str:
    facts = parking_facts_from_guest_info(property.guest_info)
    if facts:
        body = _generate_parking_body_from_facts(facts, lang, variant=variant)
        if not body:
            text_key = "parking_post_checkin" if variant == "post_checkin" else "parking"
            body = guest_text(property, text_key, lang)
    else:
        text_key = "parking_post_checkin" if variant == "post_checkin" else "parking"
        body = guest_text(property, text_key, lang)

    if not body:
        return ""

    from apps.communications.guest_parking_patterns import reservation_notes_request_parking

    if reservation_notes_request_parking(reservation_notes):
        base = (lang or "en").split("-")[0].lower()
        prefix = _PARKING_PREFIX_REQUESTED.get(base) or _PARKING_PREFIX_REQUESTED["en"]
        body = prefix + body
    return body


def build_parking_facts_for_llm(property: Property, lang: str) -> dict[str, Any]:
    facts = parking_facts_from_guest_info(property.guest_info)
    if not facts:
        return {}
    result: dict[str, Any] = {}
    for key in (
        "has_private",
        "zone_label",
        "price_per_day",
        "currency",
        "price_notes",
        "reservation_required",
        "ev_charging",
        "large_vehicles_allowed",
    ):
        if key in facts:
            result[key] = facts[key]
    custom = facts.get("custom")
    if isinstance(custom, dict):
        custom_text = _text_for_lang(custom, lang)
        if custom_text:
            result["custom"] = custom_text
    snippet = _generate_parking_body_from_facts(facts, lang, variant="standard")
    if snippet:
        result["summary"] = snippet
    post_snippet = _generate_parking_body_from_facts(facts, lang, variant="post_checkin")
    if post_snippet and post_snippet != snippet:
        result["summary_post_checkin"] = post_snippet
    return result


def merge_wifi_into_guest_info(raw: Any, *, ssid: str, password: str) -> dict[str, Any]:
    """Update facts.wifi in guest_info; preserves other keys."""
    info = normalize_guest_info(raw)
    facts = dict(info.get("facts") or {})
    wifi: dict[str, Any] = {}
    ssid_clean = (ssid or "").strip()
    password_clean = (password or "").strip()
    if ssid_clean:
        wifi["ssid"] = ssid_clean
    if password_clean:
        wifi["password"] = password_clean
    facts["wifi"] = wifi
    info["facts"] = facts
    return normalize_guest_info(info)


def format_wifi_block(property: Property, lang: str) -> str:
    """Localized WiFi lines for guest messages (empty if no SSID configured)."""
    info = normalize_guest_info(property.guest_info)
    wifi = (info.get("facts") or {}).get("wifi") or {}
    if not isinstance(wifi, dict):
        return ""
    ssid = str(wifi.get("ssid") or "").strip()
    if not ssid:
        return ""
    password = str(wifi.get("password") or "").strip()
    base = (lang or "en").split("-")[0].lower()
    labels = WIFI_LABELS.get(base) or WIFI_LABELS["en"]
    lines: list[str] = []
    instructions = wifi.get("instructions")
    if isinstance(instructions, dict):
        intro = _text_for_lang(instructions, lang)
        if intro:
            lines.append(intro)
    lines.append(f"{labels['ssid']}: {ssid}")
    if password:
        lines.append(f"{labels['password']}: {password}")
    return "\n".join(lines)


def guest_maps_url(property: Property) -> str:
    info = normalize_guest_info(property.guest_info)
    url = (info.get("links") or {}).get("maps_url") or ""
    return url or MAPS_LINK


def property_entrance_image_rel(property: Property) -> str:
    info = normalize_guest_info(property.guest_info)
    rel = (info.get("assets") or {}).get("entrance_image") or ""
    return rel or DEFAULT_ENTRANCE_IMAGE


def property_entrance_image_path(property: Property) -> Path:
    rel = property_entrance_image_rel(property)
    return Path(settings.BASE_DIR) / rel


def _localized_fact_text(facts: dict[str, Any], key: str, lang: str) -> str:
    block = facts.get(key)
    if isinstance(block, dict):
        return _text_for_lang(block, lang)
    return ""


def build_guest_facts_for_llm(property: Property, lang: str) -> dict[str, Any]:
    info = normalize_guest_info(property.guest_info)
    facts_section = info.get("facts") or {}
    result: dict[str, Any] = {}

    maps_url = guest_maps_url(property)
    if maps_url:
        result["maps_url"] = maps_url

    ai_notes = str(facts_section.get("ai_notes") or "").strip()
    if ai_notes:
        result["ai_notes"] = ai_notes

    reception_hours = str(facts_section.get("reception_hours") or "").strip()
    if reception_hours:
        result["reception_hours"] = reception_hours

    wifi = facts_section.get("wifi") or {}
    if isinstance(wifi, dict):
        wifi_facts: dict[str, str] = {}
        if wifi.get("ssid"):
            wifi_facts["ssid"] = str(wifi["ssid"])
        if wifi.get("password"):
            wifi_facts["password"] = str(wifi["password"])
        instructions = wifi.get("instructions")
        if isinstance(instructions, dict):
            instr_text = _text_for_lang(instructions, lang)
            if instr_text:
                wifi_facts["instructions"] = instr_text
        if wifi_facts:
            result["wifi"] = wifi_facts

    for fact_key in LOCALIZED_FACT_KEYS:
        text = _localized_fact_text(facts_section, fact_key, lang)
        if text:
            result[fact_key] = text

    parking_llm = build_parking_facts_for_llm(property, lang)
    if parking_llm:
        result["parking"] = parking_llm

    for text_key in (
        "entrance",
        "key_handover",
        "late_arrival",
        "breakfast",
        "tourist_tax",
    ):
        if text_key in LOCALIZED_FACT_KEYS:
            continue
        text = guest_text(property, text_key, lang)
        if text:
            result[text_key] = text

    return result
