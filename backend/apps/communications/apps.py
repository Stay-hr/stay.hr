from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.communications"
    label = "communications"

    def ready(self) -> None:
        # Avoid fail-fast validation during schema operations / bare checks.
        import sys

        skip_cmds = {
            "migrate",
            "makemigrations",
            "showmigrations",
            "collectstatic",
            "check",
        }
        if any(arg in skip_cmds for arg in sys.argv):
            return

        from apps.communications.messaging.bootstrap import bootstrap_messaging_engine

        bootstrap_messaging_engine(validate=True)
