from __future__ import annotations

from celery import shared_task

from apps.integrations.whatsapp.media_archive import archive_media, sweep_media_archive


@shared_task
def archive_whatsapp_media(archive_id: int) -> dict:
    return archive_media(archive_id)


@shared_task
def sweep_whatsapp_media_archive() -> dict:
    return sweep_media_archive()
