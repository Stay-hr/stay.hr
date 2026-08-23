"""ISO 3166-1 country catalog (shared with web/booking via data/iso3166-countries.json)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

def _countries_json_path() -> Path:
    here = Path(__file__).resolve()
    for base in (here.parents[3], here.parents[2]):
        candidate = base / "data" / "iso3166-countries.json"
        if candidate.is_file():
            return candidate
    return here.parents[3] / "data" / "iso3166-countries.json"


_COUNTRIES_JSON = _countries_json_path()


class CountryRecord(TypedDict):
    iso2: str
    iso3: str
    name_en: str
    name_hr: str


@lru_cache(maxsize=1)
def _load_countries() -> tuple[CountryRecord, ...]:
    raw = json.loads(_COUNTRIES_JSON.read_text(encoding="utf-8"))
    countries: list[CountryRecord] = []
    for row in raw:
        iso2 = str(row.get("iso2") or "").strip().upper()
        iso3 = str(row.get("iso3") or "").strip().upper()
        if len(iso2) != 2 or len(iso3) != 3:
            continue
        countries.append(
            {
                "iso2": iso2,
                "iso3": iso3,
                "name_en": str(row.get("name_en") or "").strip(),
                "name_hr": str(row.get("name_hr") or "").strip(),
            }
        )
    return tuple(countries)


@lru_cache(maxsize=1)
def _iso2_to_iso3_map() -> dict[str, str]:
    return {row["iso2"]: row["iso3"] for row in _load_countries()}


@lru_cache(maxsize=1)
def _iso3_to_iso2_map() -> dict[str, str]:
    return {row["iso3"]: row["iso2"] for row in _load_countries()}


def countries_for_select() -> list[CountryRecord]:
    return list(_load_countries())


def is_known_iso2(iso2: str) -> bool:
    code = (iso2 or "").strip().upper()
    return len(code) == 2 and code in _iso2_to_iso3_map()


def is_known_iso3(iso3: str) -> bool:
    code = (iso3 or "").strip().upper()
    return len(code) == 3 and code in _iso3_to_iso2_map()


def iso2_to_iso3(iso2: str) -> str:
    code = (iso2 or "").strip().upper()
    if len(code) != 2:
        return ""
    return _iso2_to_iso3_map().get(code, "")


def iso3_to_iso2(iso3: str) -> str:
    code = (iso3 or "").strip().upper()
    if len(code) != 3:
        return ""
    return _iso3_to_iso2_map().get(code, "")


def countries_json_path() -> Path:
    return _COUNTRIES_JSON
