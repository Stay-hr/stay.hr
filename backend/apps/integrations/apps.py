from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations"
    verbose_name = "Integrations"

    def ready(self) -> None:
        from apps.integrations import admin  # noqa: F401

        # Avoid fail-fast validation during schema operations / bare checks.
        import sys

        skip_cmds = {
            "migrate",
            "makemigrations",
            "showmigrations",
            "collectstatic",
            "check",
            "test",
        }
        if any(arg in skip_cmds for arg in sys.argv):
            return

        # Defer DB access: querying in ready() triggers Django's apps-not-ready
        # RuntimeWarning and can race with connection setup under Gunicorn.
        from django.db.backends.signals import connection_created

        from apps.integrations.whatsapp.welcome_template_config import (
            validate_welcome_templates,
        )

        state = {"done": False}

        def _validate_once(sender, connection, **kwargs) -> None:  # noqa: ARG001
            if state["done"]:
                return
            if connection.alias != "default":
                return
            state["done"] = True
            validate_welcome_templates(raise_on_error=True)

        connection_created.connect(_validate_once, weak=False)
