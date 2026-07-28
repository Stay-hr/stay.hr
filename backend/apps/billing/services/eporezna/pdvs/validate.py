from __future__ import annotations

from pathlib import Path

from lxml import etree

from apps.billing.services.eporezna.errors import PdvsValidationError
from apps.billing.services.eporezna.pdvs.builder import schema_path


def validate_pdvs_xml(xml_bytes: bytes, *, xsd_path: Path | None = None) -> None:
    """Validate ObrazacPDVS XML against the vendored XSD."""
    path = xsd_path or schema_path()
    if not path.is_file():
        raise PdvsValidationError(f"XSD not found: {path}")
    try:
        schema_doc = etree.parse(str(path))
        schema = etree.XMLSchema(schema_doc)
        doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise PdvsValidationError(f"Invalid XML: {exc}") from exc
    except etree.XMLSchemaParseError as exc:
        raise PdvsValidationError(f"Invalid XSD: {exc}") from exc

    if not schema.validate(doc):
        errors = "; ".join(str(e) for e in schema.error_log)
        raise PdvsValidationError(f"XSD validation failed: {errors}")
