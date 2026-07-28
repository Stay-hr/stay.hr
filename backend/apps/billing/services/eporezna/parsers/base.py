from __future__ import annotations

from abc import ABC, abstractmethod

from apps.billing.services.eporezna.dto import ParsedForeignServiceInvoice


class ForeignServiceInvoiceParser(ABC):
    """One document layout / version. Multiple parsers may map to the same provider."""

    name: str

    @classmethod
    @abstractmethod
    def can_parse(cls, *, filename: str, raw: bytes, text: str | None) -> bool:
        """Return True if this parser should handle the document."""

    @abstractmethod
    def parse(self, raw: bytes) -> ParsedForeignServiceInvoice:
        """Extract invoice fields; raise ParseError on failure."""
