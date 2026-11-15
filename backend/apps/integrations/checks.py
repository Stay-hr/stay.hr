"""Django system checks for the WhatsApp media archive."""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Warning, register


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


@register()
def check_private_media_root(app_configs, **kwargs):
    private = getattr(settings, "PRIVATE_MEDIA_ROOT", None)
    if not private:
        return []
    errors = []
    private_path = Path(private).resolve()
    for label in ("MEDIA_ROOT", "STATIC_ROOT"):
        public = getattr(settings, label, None)
        if public and _overlaps(private_path, Path(public).resolve()):
            errors.append(
                Error(
                    f"PRIVATE_MEDIA_ROOT ({private_path}) overlaps {label} ({public}).",
                    hint=(
                        f"{label} may be served publicly. Archived WhatsApp media (guest "
                        "documents) must live in a separate private directory."
                    ),
                    id="integrations.E001",
                )
            )
    if getattr(settings, "WHATSAPP_MEDIA_ARCHIVE_ENABLED", False):
        if not (private_path.is_dir() and os.access(private_path, os.W_OK | os.X_OK)):
            errors.append(
                Warning(
                    f"WhatsApp media archive is enabled but {private_path} is missing or not "
                    "writable by this process.",
                    hint=(
                        "Create the host directory and `chown 1000:1000` + `chmod 700` it "
                        "(container user is uid 1000). Downloads are deferred until fixed."
                    ),
                    id="integrations.W001",
                )
            )
    return errors
