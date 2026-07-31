# Unit photos — hel1 smoke (ADR 0015 Phase B)

Project pending `PhotoOutbox` to Channex on **hel1** (sole writer). WSL stays read-only (`CHANNEX_OUTBOUND_ENABLED=false`).

After this smoke passes, ADR 0015 through Phase B is **ops-validated**. Phase C (Reception UI) is a separate track — same service / outbox / provider, no pipeline rewrite.

## Prerequisites

- Migrations applied (`0020_unit_photos_adr0015`, `0021_unitphotolink_tombstone`)
- `CHANNEX_OUTBOUND_ENABLED=true` on hel1
- Active Channex `IntegrationConfig` for `uzorita` with R4 `room_types` mapping
- R4 photos available (e.g. `.wp_photos/R4/`)

## 1. Import (no Channex yet)

```bash
python manage.py import_unit_photos \
  --tenant-slug uzorita \
  --unit-code R4 \
  --dir .wp_photos/R4 \
  --primary r4-19.jpeg
```

Must finish **without validation errors**.

### DB gate (before flush)

Expect for unit R4 (adjust unit id if needed):

| Check | Expect |
|-------|--------|
| `UnitPhoto` (non-deleted) | **21** |
| `PhotoOutbox(status=pending)` for those photos | **≥ 21** (first photo also enqueues `SET_PRIMARY`) |

```python
# django shell on hel1
from apps.tenants.models import Tenant
from apps.properties.models import Unit, UnitPhoto, PhotoOutbox

t = Tenant.objects.get(slug="uzorita")
u = Unit.objects.get(tenant=t, code="R4")
photos = UnitPhoto.objects.filter(unit=u).exclude(status="deleted")
print("UnitPhoto", photos.count())  # 21
print(
    "pending outbox",
    PhotoOutbox.objects.filter(unit_photo__unit=u, status="pending").count(),
)
```

## 2. Flush to Channex

```bash
python manage.py flush_photo_outbox --tenant-slug uzorita
# or Celery: flush_photo_outbox_task.delay("uzorita")
```

### DB gate (after flush)

| Check | Expect |
|-------|--------|
| Active `UnitPhotoLink` (`deleted_at` null) for R4 photos | **21** |
| All those `UnitPhoto.status` | **`active`** |
| Outbox for R4 | **no `pending`**, **no `failed`** |

```python
from apps.properties.models import UnitPhotoLink

links = UnitPhotoLink.objects.filter(
    unit_photo__unit=u, provider="channex", deleted_at__isnull=True
)
print("links", links.count())  # 21
print("statuses", set(photos.values_list("status", flat=True)))  # {"active"}
print(
    "pending/failed",
    PhotoOutbox.objects.filter(
        unit_photo__unit=u, status__in=["pending", "failed"]
    ).count(),
)  # 0
```

## 3. Channex UI

Standard King Room R4 gallery: **21** photos, correct primary (cover = `position=0`).

## 4. Idempotent re-flush (strongest proof)

```bash
python manage.py flush_photo_outbox --tenant-slug uzorita
```

Confirm:

- no new Channex uploads
- no new `external_id` values
- no new `UnitPhotoLink` rows
- logs: `photo_upload_skipped_total` increases; `photo_upload_success_total` unchanged vs first flush

Optional read-only LIST (smoke only — **not** part of upload path):

```python
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig

row = get_active_channex_integration("uzorita")
cfg = ChannexRuntimeConfig.from_integration_dict(row.get_config_dict())
with ChannexClient(cfg) as c:
    remote = c.list_photos(
        property_id=cfg.property_id,
        room_type_id=cfg.room_type_id_for_unit_code("R4"),
    )
    print(len(remote))  # 21
```

## 5. Booking.com (observe)

Wait for channel Photos sync; visual spot-check. Do **not** edit the OTA gallery.

Confirm:

- all **21** photos visible
- cover / main photo is **`r4-19.jpeg`**
- no obvious duplicates or wrong order

When that passes, mark Gate B3 fully closed and ADR 0015 **Production Proven** (see result log).

## Lessons learned — post-upload verify

Channex may renumber `position` on create. **Do not** hard-fail upload verify on position alone.

Provider verification validates object identity and target (`external_id`, `room_type_id`); gallery ordering is eventually established by explicit `SET_PRIMARY` / `REORDER`. See ADR 0015 Phase B lessons learned.

## WSL

Do **not** flush with write enabled against live Channex. If hel1 is offline and you must force:

```bash
python manage.py flush_photo_outbox --tenant-slug uzorita --force-channex-outbound
```

## Metrics to watch (logs)

`photo_upload_success_total`, `photo_upload_failed_total`, `photo_upload_skipped_total`, `photo_delete_total`, `photo_reorder_total`, `photo_outbox_pending`.

## After smoke PASS

- Phase 0 ADR ✅ · Phase A domain ✅ · Phase B Channex projection ✅
- Close this vertical; open **Phase C** (Reception upload/reorder/delete UI) as a separate stream on the same pipeline.

---

## Gate B3 result log

| Date | Result | Notes |
|------|--------|-------|
| 2026-07-31 | **PASS (automated)** — Booking visual still ops-confirm | hel1 `7403b01`. Import 21 OK. Flush 21 links / all `ACTIVE` / 0 pending\|failed. Channex LIST=21; primary `r4-19.jpeg` remote `position=0`. Idempotent re-enqueue: 21× `photo_upload_skipped_total`, `external_id` unchanged. Soft position verify (hard-fail only id/room_type). **Booking.com gallery: confirm visually before calling ADR production-proven closed.** |

### Formal freeze until Booking confirm

| Status | State |
|--------|-------|
| ADR 0015 | Accepted |
| Phase A | Complete |
| Phase B | Code complete + production validated to Channex |
| Booking propagation | Final observational check |
| Phase C | Blocked until Booking confirmation |

No further architecture / domain / provider / worker changes until Booking spot-check (21 / `r4-19` cover / no duplicates) → then mark **ADR 0015 — Production Proven** and open a separate Phase C PR.
