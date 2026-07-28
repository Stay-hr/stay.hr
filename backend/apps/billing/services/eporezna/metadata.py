"""Shared Metapodaci serialization for ePorezna form exports.

Invariant: Metadata must be byte-identical across all ePorezna exports
except for Naslov, Uskladjenost, timestamp and UUID.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from lxml import etree

META_NS = "http://e-porezna.porezna-uprava.hr/sheme/Metapodaci/v2-0"

_ADRESANT = "Ministarstvo Financija, Porezna uprava, Zagreb"


class EporeznaMetadataBuilder:
    """Append the standard Metapodaci block; callers vary only title / conforms_to."""

    def append(
        self,
        root: etree._Element,
        *,
        title: str,
        conforms_to: str,
        autor: str,
        now: datetime,
        ident: UUID,
    ) -> etree._Element:
        meta = etree.SubElement(
            root,
            f"{{{META_NS}}}Metapodaci",
            nsmap={None: META_NS},
        )

        def field(tag: str, dc: str, text: str) -> None:
            el = etree.SubElement(meta, f"{{{META_NS}}}{tag}")
            el.set("dc", dc)
            el.text = text

        field("Naslov", "http://purl.org/dc/elements/1.1/title", title)
        field("Autor", "http://purl.org/dc/elements/1.1/creator", autor)
        field(
            "Datum",
            "http://purl.org/dc/elements/1.1/date",
            now.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        field("Format", "http://purl.org/dc/elements/1.1/format", "text/xml")
        field("Jezik", "http://purl.org/dc/elements/1.1/language", "hr-HR")
        field(
            "Identifikator",
            "http://purl.org/dc/elements/1.1/identifier",
            str(ident),
        )
        field(
            "Uskladjenost",
            "http://purl.org/dc/terms/conformsTo",
            conforms_to,
        )
        field("Tip", "http://purl.org/dc/elements/1.1/type", "Elektronički obrazac")
        adresant = etree.SubElement(meta, f"{{{META_NS}}}Adresant")
        adresant.text = _ADRESANT
        return meta
