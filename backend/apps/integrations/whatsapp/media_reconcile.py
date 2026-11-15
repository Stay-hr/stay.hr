"""Operator-driven reconciliation between the archive table and the private tree.

Report-only unless ``delete=True``. Never runs from beat: removing files that no row
references is an explicit operator decision (they may be the only copy of a document).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

from apps.integrations.models import WhatsAppMediaArchive, WhatsAppMediaFileDeletion
from apps.integrations.whatsapp.media_file_cleanup import ARCHIVE_PREFIX

Status = WhatsAppMediaArchive.Status


@dataclass
class ReconcileReport:
    orphan_files: list[str] = field(default_factory=list)
    part_files: list[str] = field(default_factory=list)
    missing_files: list[int] = field(default_factory=list)  # archive ids archived w/o file
    deleted: list[str] = field(default_factory=list)


def reconcile_orphans(*, min_age_hours: float = 24.0, delete: bool = False) -> ReconcileReport:
    root = Path(settings.PRIVATE_MEDIA_ROOT)
    report = ReconcileReport()
    base = root / ARCHIVE_PREFIX.rstrip("/")

    referenced = set(
        WhatsAppMediaArchive.objects.exclude(file="").values_list("file", flat=True)
    )
    # Files already queued for deletion are not orphans, they are in flight.
    in_flight = set(
        WhatsAppMediaFileDeletion.objects.filter(status="pending").values_list("file_name", flat=True)
    )
    cutoff = time.time() - min_age_hours * 3600

    if base.is_dir():
        for dirpath, _dirs, files in os.walk(base):
            for fname in files:
                full = Path(dirpath) / fname
                name = full.relative_to(root).as_posix()
                if name in referenced or name in in_flight:
                    continue
                try:
                    if full.stat().st_mtime > cutoff:
                        continue  # too recent: may belong to a running attempt
                except OSError:
                    continue
                (report.part_files if fname.endswith(".part") else report.orphan_files).append(name)

    for row in WhatsAppMediaArchive.objects.filter(status=Status.ARCHIVED).exclude(file="").iterator():
        if not row.file.storage.exists(row.file.name):
            report.missing_files.append(row.pk)

    if delete:
        for name in report.orphan_files + report.part_files:
            # Re-check right before unlinking: a row may have claimed the name meanwhile.
            if WhatsAppMediaArchive.objects.filter(file=name).exists():
                continue
            try:
                (root / name).unlink()
                report.deleted.append(name)
            except OSError:
                pass
    return report
