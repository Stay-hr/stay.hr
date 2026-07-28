from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"

    def ready(self) -> None:
        from apps.billing.services.eporezna.parsers.bootstrap import (
            bootstrap_invoice_parsers,
        )

        bootstrap_invoice_parsers()
