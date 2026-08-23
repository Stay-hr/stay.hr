from __future__ import annotations

import logging

from apps.core.countries import iso2_to_iso3 as local_iso2_to_iso3
from apps.core.countries import is_known_iso3
from apps.integrations.evisitor.client import EvisitorClient
from apps.integrations.evisitor.config import EvisitorRuntimeConfig
from apps.integrations.evisitor.exceptions import EvisitorApiError, EvisitorConfigError

logger = logging.getLogger(__name__)

_DOCUMENT_TYPE_MAP = {
    "passport": "008",
    "putovnica": "008",
    "putovnica.": "008",
    "id": "027",
    "identity": "027",
    "osobna": "027",
    "osobna iskaznica": "027",
    "identity card": "027",
}

# Provider-specific ISO3 codes outside ISO 3166 (e.g. Kosovo in MRZ).
_EVISITOR_SPECIFIC_ISO3 = frozenset({"XXK"})

_country_cache: dict[tuple[str, int | None], dict[str, str]] = {}


def _cache_key(config: EvisitorRuntimeConfig, property_id: int | None) -> tuple[str, int | None]:
    return (config.username, property_id)


def _iso2_to_iso3_map(config: EvisitorRuntimeConfig, property_id: int | None) -> dict[str, str]:
    key = _cache_key(config, property_id)
    if key in _country_cache:
        return _country_cache[key]
    if not config.enabled:
        return {}
    try:
        with EvisitorClient(config) as client:
            client.login()
            records = client.fetch_records(
                "Country",
                psize=300,
                filters=[{"Property": "Active", "Operation": "equal", "Value": "true"}],
            )
        mapping: dict[str, str] = {}
        for row in records:
            iso2 = (row.get("CodeTwoLetters") or "").strip().upper()
            iso3 = (row.get("CodeThreeLetters") or "").strip().upper()
            if iso2 and iso3:
                mapping[iso2] = iso3
        _country_cache[key] = mapping
        return mapping
    except (EvisitorConfigError, EvisitorApiError) as exc:
        logger.warning("eVisitor country lookup failed: %s", exc)
        return {}


def iso2_to_iso3(
    iso2: str,
    *,
    config: EvisitorRuntimeConfig | None = None,
    property_id: int | None = None,
) -> str:
    code = (iso2 or "").strip().upper()
    if len(code) == 3:
        if is_known_iso3(code):
            return code
        if code in _EVISITOR_SPECIFIC_ISO3:
            return code
        return ""
    if len(code) != 2:
        return ""

    mapped = local_iso2_to_iso3(code)
    if mapped:
        return mapped

    if config is not None:
        provider_mapped = _iso2_to_iso3_map(config, property_id).get(code)
        if provider_mapped:
            return provider_mapped
    return ""


def map_document_type_code(document_type: str, document_code: str = "") -> str:
    raw = f"{document_type} {document_code}".strip().lower()
    if not raw:
        return ""
    for key, mapped in _DOCUMENT_TYPE_MAP.items():
        if key in raw:
            return mapped
    if document_code.strip().isdigit():
        return document_code.strip()
    return ""
