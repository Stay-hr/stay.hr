from __future__ import annotations


class ChannexConfigError(Exception):
    pass


class ChannexApiError(Exception):
    """Channex HTTP/API failure. ``status_code`` is set for HTTP responses."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChannexBookingIngestError(Exception):
    pass


class ChannexWriteDisabled(Exception):
    """Raised when a non-GET Channex API call is blocked by write capability guard."""

    def __init__(
        self,
        message: str = "Channex write disabled (CHANNEX_OUTBOUND_ENABLED=false).",
        *,
        method: str | None = None,
        path: str | None = None,
        reason: str = "feature_flag",
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.reason = reason


class PhotoSyncPermanentError(Exception):
    """Photo outbox failure that must not be retried (4xx mapping/auth/validation)."""


class PhotoSyncRetryableError(Exception):
    """Photo outbox failure that should be retried (429/5xx/timeout/network)."""
