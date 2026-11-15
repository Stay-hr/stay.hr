"""Shared fixtures for WhatsApp media archive tests (no tests defined here)."""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.integrations.models import IntegrationConfig, WhatsAppMediaArchive, WhatsAppMessage
from apps.integrations.whatsapp.media_archive import enqueue_archive as REAL_ENQUEUE  # noqa: F401 (pre-patch ref)
from apps.integrations.whatsapp.media_download import MediaDownloadResult
from apps.tenants.models import Tenant

PHONE_NUMBER_ID = "7794189252778687"


def media_payload(kind: str = "image", media_id: str = "media-1", **extra) -> dict:
    block = {"id": media_id, "mime_type": "image/jpeg", **extra}
    return {"type": kind, kind: block}


class ArchiveTestBase(TestCase):
    """Archive enabled, private root in a temp dir, Celery enqueue mocked."""

    settings_overrides: dict = {}

    def setUp(self):
        root = tempfile.mkdtemp(prefix="stay-private-media-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.private_root = root
        overrides = override_settings(
            PRIVATE_MEDIA_ROOT=root,
            WHATSAPP_MEDIA_ARCHIVE_ENABLED=True,
            WHATSAPP_MEDIA_MAX_BYTES=1024 * 1024,
            WHATSAPP_MEDIA_MAX_ATTEMPTS=3,
            WHATSAPP_MEDIA_MAX_TOTAL_BYTES=0,
            WHATSAPP_MEDIA_MIN_FREE_BYTES=0,
            **self.settings_overrides,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

        enqueue = patch("apps.integrations.whatsapp.media_archive.enqueue_archive")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

        self.tenant = Tenant.objects.create(slug="media-hotel", name="Media Hotel", default_language="hr")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key=PHONE_NUMBER_ID,
            is_active=True,
        )

    def make_message(
        self,
        *,
        kind: str = "image",
        wamid: str = "wamid.1",
        media_id: str = "media-1",
        direction: str = WhatsAppMessage.Direction.INBOUND,
        **media_extra,
    ) -> WhatsAppMessage:
        return WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid=wamid,
            wa_id="385911111111",
            phone_number_id=PHONE_NUMBER_ID,
            direction=direction,
            message_type=kind,
            raw_payload=media_payload(kind, media_id, **media_extra),
        )

    def make_archive(self, **kwargs) -> WhatsAppMediaArchive:
        message = kwargs.pop("message", None) or self.make_message()
        from apps.integrations.whatsapp.media_archive import ensure_media_archive

        archive, _ = ensure_media_archive(message)
        for key, value in kwargs.items():
            setattr(archive, key, value)
        if kwargs:
            archive.save()
        return archive


def fake_fetch(content: bytes = b"binary-media", mime: str = "image/jpeg"):
    """Side effect for fetch_whatsapp_media_to_file: writes ``content`` into dest."""
    import hashlib

    def _fetch(*, media_id, dest, max_bytes, access_token=None, api_version=None):
        dest.write(content)
        return MediaDownloadResult(
            mime_type=mime,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    return _fetch
