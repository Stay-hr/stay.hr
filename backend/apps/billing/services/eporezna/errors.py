"""Domain errors for foreign-service invoice import and PDV-S export."""

from __future__ import annotations


class EporeznaError(Exception):
    """Base error for ePorezna / foreign-service invoice flows."""


class UnknownParserError(EporeznaError):
    """No registered parser accepted the document."""


class ParseError(EporeznaError):
    """Parser could not extract a valid invoice from the document."""


class InvoiceValidationError(EporeznaError):
    """Parsed DTO failed domain validation."""


class InvoiceConflictError(EporeznaError):
    """Same provider invoice number already exists with a different document."""


class PdvsBuildError(EporeznaError):
    """Cannot build PDV-S XML (missing fiscal settings, preparer, etc.)."""


class PdvsValidationError(EporeznaError):
    """Generated or uploaded XML failed XSD / structural validation."""
