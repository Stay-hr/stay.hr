"""Ordered parser registry — first ``can_parse`` wins (supports V1/V2 layouts)."""

from __future__ import annotations

from typing import Iterable

from apps.billing.services.eporezna.errors import UnknownParserError
from apps.billing.services.eporezna.parsers.base import ForeignServiceInvoiceParser


class InvoiceParserRegistry:
    def __init__(self) -> None:
        self._parsers: list[type[ForeignServiceInvoiceParser]] = []

    def clear(self) -> None:
        self._parsers.clear()

    def register(self, parser_cls: type[ForeignServiceInvoiceParser]) -> None:
        if not getattr(parser_cls, "name", None):
            raise ValueError("Parser class must define name")
        if any(p.name == parser_cls.name for p in self._parsers):
            raise ValueError(f"Duplicate parser name: {parser_cls.name!r}")
        self._parsers.append(parser_cls)

    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._parsers)

    def all(self) -> tuple[type[ForeignServiceInvoiceParser], ...]:
        return tuple(self._parsers)

    def detect(
        self,
        *,
        filename: str,
        raw: bytes,
        text: str | None = None,
    ) -> ForeignServiceInvoiceParser:
        for parser_cls in self._parsers:
            if parser_cls.can_parse(filename=filename, raw=raw, text=text):
                return parser_cls()
        raise UnknownParserError(
            f"No parser accepted document {filename!r} "
            f"(registered: {', '.join(self.names()) or 'none'})"
        )

    def __len__(self) -> int:
        return len(self._parsers)

    def __iter__(self) -> Iterable[type[ForeignServiceInvoiceParser]]:
        return iter(self._parsers)


invoice_parser_registry = InvoiceParserRegistry()
