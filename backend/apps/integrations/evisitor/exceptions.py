class EvisitorError(Exception):
    """Base eVisitor integration error."""


class EvisitorConfigError(EvisitorError):
    """Missing or invalid configuration."""


class EvisitorValidationError(EvisitorError):
    def __init__(self, message: str, field_errors: dict | None = None):
        super().__init__(message)
        self.field_errors = field_errors or {}


class EvisitorApiError(EvisitorError):
    def __init__(
        self,
        message: str,
        *,
        user_message: str = "",
        system_message: str = "",
        status_code: int | None = None,
        failed_guests: list[dict] | None = None,
    ):
        super().__init__(message)
        self.user_message = user_message
        self.system_message = system_message
        self.status_code = status_code
        self.failed_guests = list(failed_guests or [])
