"""Domain errors for foreign-service invoice import and ePorezna export."""

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
    """Generated or uploaded PDV-S XML failed XSD / structural validation."""


class PdvBuildError(EporeznaError):
    """Cannot build Obrazac PDV XML (missing fiscal settings, source data, etc.)."""


class PdvValidationError(EporeznaError):
    """Generated or uploaded Obrazac PDV XML failed XSD / structural validation."""
