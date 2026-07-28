from __future__ import annotations

import logging

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

_ISO2_FALLBACKS = {
    "HR": "HRV",
    "DE": "DEU",
    "IT": "ITA",
    "AT": "AUT",
    "SI": "SVN",
    "BE": "BEL",
    "FR": "FRA",
    "NL": "NLD",
    "GB": "GBR",
    "US": "USA",
    "CH": "CHE",
    "PL": "POL",
    "CZ": "CZE",
    "SK": "SVK",
    "HU": "HUN",
    "RS": "SRB",
    "BA": "BIH",
    "ME": "MNE",
    "MK": "MKD",
    "AL": "ALB",
    "GR": "GRC",
    "ES": "ESP",
    "PT": "PRT",
    "SE": "SWE",
    "NO": "NOR",
    "DK": "DNK",
    "FI": "FIN",
    "IE": "IRL",
    "LU": "LUX",
    "IN": "IND",
    "CO": "COL",
    "LT": "LTU",
    "LV": "LVA",
    "EE": "EST",
}

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
        return code
    if len(code) != 2:
        return ""
    if config is not None:
        mapped = _iso2_to_iso3_map(config, property_id).get(code)
        if mapped:
            return mapped
    return _ISO2_FALLBACKS.get(code, "")


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
