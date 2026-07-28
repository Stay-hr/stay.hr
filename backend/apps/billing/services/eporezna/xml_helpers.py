"""Small ElementTree helpers shared by ePorezna form builders."""

from __future__ import annotations

from decimal import Decimal

from lxml import etree

TWOPLACES = Decimal("0.01")


def append_text(
    parent: etree._Element,
    tag: str,
    text: str,
    *,
    ns: str,
) -> etree._Element:
    el = etree.SubElement(parent, f"{{{ns}}}{tag}")
    el.text = text
    return el


def append_decimal(
    parent: etree._Element,
    tag: str,
    value: Decimal,
    *,
    ns: str,
) -> etree._Element:
    quantized = value.quantize(TWOPLACES)
    return append_text(parent, tag, f"{quantized:.2f}", ns=ns)


def append_bool(
    parent: etree._Element,
    tag: str,
    value: bool,
    *,
    ns: str,
) -> etree._Element:
    return append_text(parent, tag, "true" if value else "false", ns=ns)


def money(value: Decimal) -> str:
    return f"{value.quantize(TWOPLACES):.2f}"
