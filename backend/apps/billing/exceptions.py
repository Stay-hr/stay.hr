class BillingError(Exception):
    """Base billing error."""


class InvoiceBuildError(BillingError):
    """Cannot build invoice from reservation."""


class FiscalConfigError(BillingError):
    """Tenant fiscal settings incomplete or invalid."""


class BillingRecipientError(BillingError):
    """Open billing-recipient create/update rejected."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class BillingRecipientIssuanceDeferred(BillingError):
    """Invoice issuance deferred until the billing recipient is complete."""

    code = "billing_recipient_incomplete"


class InvoiceGraphError(BillingError):
    """Replacement/invoice graph is invalid or ambiguous; fail closed."""


class InvoiceReplacementError(BillingError):
    """Invoice replacement command or contract rejected."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ReplacementInProgress(BillingError):
    """Ordinary issuance blocked in the post-storno replacement gap."""

    code = "replacement_in_progress"


class FiscalizationError(BillingError):
    """Fiskalizacija 1.0 request failed."""

    def __init__(self, message: str, *, fiskal_request_id=None) -> None:
        super().__init__(message)
        self.fiskal_request_id = fiskal_request_id
