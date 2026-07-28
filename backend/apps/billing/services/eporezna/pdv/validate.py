from __future__ import annotations

from pathlib import Path

from lxml import etree

from apps.billing.services.eporezna.errors import PdvValidationError
from apps.billing.services.eporezna.pdv.builder import schema_path


def validate_pdv_xml(xml_bytes: bytes, *, xsd_path: Path | None = None) -> None:
    """Validate ObrazacPDV XML against the vendored XSD."""
    path = xsd_path or schema_path()
    if not path.is_file():
        raise PdvValidationError(f"XSD not found: {path}")
    try:
        schema_doc = etree.parse(str(path))
        schema = etree.XMLSchema(schema_doc)
        doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise PdvValidationError(f"Invalid XML: {exc}") from exc
    except etree.XMLSchemaParseError as exc:
        raise PdvValidationError(f"Invalid XSD: {exc}") from exc

    if not schema.validate(doc):
        errors = "; ".join(str(e) for e in schema.error_log)
        raise PdvValidationError(f"XSD validation failed: {errors}")
