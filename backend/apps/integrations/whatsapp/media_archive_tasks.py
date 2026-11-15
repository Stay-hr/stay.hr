from __future__ import annotations

from celery import shared_task

from apps.integrations.whatsapp.media_archive import (
    TASK_SOFT_TIME_LIMIT,
    TASK_TIME_LIMIT,
    archive_media,
    sweep_media_archive,
)


# Limits sit below the row lease (see media_archive timing contract): a worker that is
# still running can never have its claim reclaimed by the sweeper.
@shared_task(soft_time_limit=TASK_SOFT_TIME_LIMIT, time_limit=TASK_TIME_LIMIT)
def archive_whatsapp_media(archive_id: int) -> dict:
    return archive_media(archive_id)


@shared_task
def sweep_whatsapp_media_archive() -> dict:
    return sweep_media_archive()
