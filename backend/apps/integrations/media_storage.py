"""Private (non-public) file storage for archived WhatsApp media.

``MEDIA_ROOT`` is routed publicly by Traefik, so archived media lives in a separate
``PRIVATE_MEDIA_ROOT`` and has no URL: ``storage.url()`` raises. Files are only ever
read through an authenticated view (PR 3).
"""

from __future__ import annotations

import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateMediaStorage(FileSystemStorage):
    def __init__(self) -> None:
        # allow_overwrite: archive file names are deterministic ({archive_id}-{sha12}.ext), so
        # a retry after a crash rewrites the same path instead of leaving a suffixed
        # duplicate behind. Publication is atomic (part file + os.replace), see media_archive.
        super().__init__(
            file_permissions_mode=0o600,
            directory_permissions_mode=0o700,
            allow_overwrite=True,
        )

    @property
    def base_location(self) -> str:
        return str(settings.PRIVATE_MEDIA_ROOT)

    @property
    def location(self) -> str:
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):  # type: ignore[override]
        return None


def get_private_media_storage() -> PrivateMediaStorage:
    return PrivateMediaStorage()
