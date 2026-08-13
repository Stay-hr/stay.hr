from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone as dt_timezone
from pathlib import PurePosixPath
from typing import Any

from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.integrations.channex.booking_service import (
    _channex_booking_lookup_codes,
    _resolve_channex_booking_payload,
    channex_external_id,
    parse_channex_booking_id,
)
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.models import ChannexBookingRevision, ChannexMessage, IntegrationConfig
from apps.reservations.models import Reservation, ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def find_reservation_for_channex_booking(tenant: Tenant, booking_id: str) -> Reservation | None:
    booking_id = (booking_id or "").strip()
    if not booking_id:
        return None

    reservation = Reservation.objects.filter(
        tenant=tenant,
        external_id=channex_external_id(booking_id),
    ).first()
    if reservation is not None:
        return reservation

    revision = (
        ChannexBookingRevision.objects.filter(tenant=tenant, booking_id=booking_id)
        .select_related("reservation")
        .first()
    )
    if revision is not None and revision.reservation_id is not None:
        return revision.reservation

    return None


def resolve_reservation_for_channex_message(
    tenant: Tenant,
    *,
    booking_id: str = "",
    ota_reservation_id: str = "",
) -> Reservation | None:
    """Match inbound Channex message to a stay.hr reservation."""
    booking_id = (booking_id or "").strip()
    ota_reservation_id = (ota_reservation_id or "").strip()

    if booking_id:
        reservation = find_reservation_for_channex_booking(tenant, booking_id)
        if reservation is not None:
            return reservation

    for code in (ota_reservation_id,):
        if not code:
            continue
        reservation = Reservation.objects.filter(tenant=tenant, booking_code=code).first()
        if reservation is not None:
            return reservation
        reservation = Reservation.objects.filter(tenant=tenant, external_id=code).first()
        if reservation is not None:
            return reservation

    return None


def repair_reservation_channex_external_id(
    reservation: Reservation,
    channex_booking_id: str,
) -> bool:
    """Set external_id to channex:{uuid} when legacy row used OTA code only."""
    channex_booking_id = (channex_booking_id or "").strip()
    if not channex_booking_id:
        return False
    target = channex_external_id(channex_booking_id)
    if reservation.external_id == target:
        return False
    if parse_channex_booking_id(reservation.external_id):
        return False
    reservation.external_id = target
    reservation.save(update_fields=["external_id", "updated_at"])
    logger.info(
        "repaired reservation Channex external_id",
        extra={
            "reservation_id": reservation.pk,
            "booking_code": reservation.booking_code,
            "external_id": target,
        },
    )
    return True


def resolve_channex_booking_id_for_reservation(
    integration_row: IntegrationConfig,
    reservation: Reservation,
    *,
    client: ChannexClient | None = None,
    repair_external_id: bool = True,
) -> str | None:
    """Resolve Channex booking UUID for API calls; optionally repair legacy external_id."""
    booking_id = parse_channex_booking_id(reservation.external_id)
    if booking_id:
        return booking_id

    revision = (
        ChannexBookingRevision.objects.filter(reservation=reservation)
        .order_by("-id")
        .first()
    )
    if revision is not None and (revision.booking_id or "").strip():
        booking_id = revision.booking_id.strip()
        if repair_external_id:
            repair_reservation_channex_external_id(reservation, booking_id)
        return booking_id

    if not _channex_booking_lookup_codes(reservation):
        return None

    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)
    try:
        resolved = _resolve_channex_booking_payload(client, reservation)
    finally:
        if owns_client and client is not None:
            client.close()

    if resolved is None:
        return None

    booking_id, _payload, lookup_method = resolved
    if repair_external_id and lookup_method == "booking_code":
        repair_reservation_channex_external_id(reservation, booking_id)
    return booking_id or None


def relink_unlinked_channex_messages(tenant: Tenant) -> int:
    """Retry reservation matching for messages stored without a link."""
    updated = 0
    qs = ChannexMessage.objects.filter(tenant=tenant, reservation__isnull=True)
    for row in qs.iterator():
        flat = row.raw_payload if isinstance(row.raw_payload, dict) else {}
        ota = str(
            flat.get("ota_reservation_code") or flat.get("ota_reservation_id") or ""
        ).strip()
        reservation = resolve_reservation_for_channex_message(
            tenant,
            booking_id=row.channex_booking_id or "",
            ota_reservation_id=ota,
        )
        if reservation is None:
            continue
        row.reservation = reservation
        row.save(update_fields=["reservation"])
        updated += 1
    return updated


def _reservation_can_sync_messages(reservation: Reservation) -> bool:
    if reservation.import_source != "channex":
        return False
    if parse_channex_booking_id(reservation.external_id):
        return True
    if _channex_booking_lookup_codes(reservation):
        return True
    return ChannexBookingRevision.objects.filter(reservation=reservation).exists()


def channex_message_id_from_payload(payload: dict[str, Any]) -> str:
    for key in ("id", "ota_message_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    raise ChannexBookingIngestError("Channex message payload missing id.")


def _flatten_message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    attrs = payload.get("attributes")
    if isinstance(attrs, dict):
        merged = dict(attrs)
        if payload.get("id") and not merged.get("id"):
            merged["id"] = payload["id"]
        thread = (payload.get("relationships") or {}).get("message_thread") or {}
        thread_data = thread.get("data") if isinstance(thread, dict) else None
        if isinstance(thread_data, dict) and thread_data.get("id"):
            merged.setdefault("message_thread_id", str(thread_data["id"]))
        return merged
    return payload


def _message_created_at(payload: dict[str, Any]) -> datetime | None:
    for key in ("inserted_at", "created_at"):
        raw = payload.get(key)
        if not raw:
            continue
        parsed = parse_datetime(str(raw).replace("Z", "+00:00"))
        if parsed is None:
            continue
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        return parsed
    return None


def _message_body(payload: dict[str, Any]) -> str:
    return str(payload.get("message") or payload.get("body") or "").strip()


def _attachment_paths(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("attachments")
    if not isinstance(raw, list):
        return []
    paths: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            paths.append(item.strip())
        elif isinstance(item, dict):
            link = str(item.get("url") or item.get("link") or "").strip()
            if link:
                paths.append(link)
    return paths


def first_attachment_path(payload: dict[str, Any]) -> str | None:
    paths = _attachment_paths(payload)
    return paths[0] if paths else None


def _channex_attachment_filename(path: str, content_type: str) -> str:
    name = PurePosixPath(path.split("?", 1)[0]).name
    if name and "." in name:
        return name[:200]
    mime = (content_type or "").split(";", 1)[0].strip()
    ext = mimetypes.guess_extension(mime) or ".bin"
    return f"attachment{ext}"


def ensure_channex_message_media(
    row: ChannexMessage,
    *,
    client: ChannexClient | None = None,
) -> bool:
    """Store the first Channex attachment on ``media_file`` if missing.

    Ingest-only. GET must not call this. Failures are logged and do not raise.
    """
    if getattr(row, "media_file", None) and row.media_file:
        return True
    path = first_attachment_path(row.raw_payload or {})
    if not path:
        if row.have_attachment:
            logger.warning(
                "channex media ingest skipped missing path",
                extra={
                    "channex_message_id": row.channex_message_id,
                    "reservation_id": row.reservation_id,
                },
            )
        return False

    owns_client = client is None
    active_client = client
    try:
        if active_client is None:
            integration = row.integration
            if integration is None:
                from apps.integrations.channex.ari_service import get_active_channex_integration

                integration = get_active_channex_integration(row.tenant.slug)
            config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
            active_client = ChannexClient(config)
        file_bytes, content_type = active_client.fetch_attachment_bytes(path)
        if not file_bytes:
            logger.warning(
                "channex media ingest empty body",
                extra={
                    "channex_message_id": row.channex_message_id,
                    "reservation_id": row.reservation_id,
                },
            )
            return False
        filename = _channex_attachment_filename(path, content_type)
        row.have_attachment = True
        row.media_file.save(filename, ContentFile(file_bytes), save=False)
        row.save(update_fields=["have_attachment", "media_file"])
        return True
    except (ChannexApiError, ChannexBookingIngestError) as exc:
        logger.warning(
            "channex media ingest failed",
            extra={
                "channex_message_id": row.channex_message_id,
                "reservation_id": row.reservation_id,
                "error": str(exc),
            },
        )
        return False
    finally:
        if owns_client and active_client is not None:
            active_client.close()


def _message_sender(payload: dict[str, Any]) -> str:
    sender = str(payload.get("sender") or "").strip().lower()
    if sender == ChannexMessage.Sender.GUEST:
        return ChannexMessage.Sender.GUEST
    return ChannexMessage.Sender.PROPERTY


def _direction_for_sender(sender: str) -> str:
    if sender == ChannexMessage.Sender.GUEST:
        return ChannexMessage.Direction.INBOUND
    return ChannexMessage.Direction.OUTBOUND


def _extract_message_rows(api_response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in api_response.get("data") or []:
        if isinstance(item, dict):
            rows.append(_flatten_message_payload(item))
    data = api_response.get("data")
    if isinstance(data, dict):
        rows.append(_flatten_message_payload(data))
    return rows


def _channex_message_visible_in_timeline(row: ChannexMessage) -> bool:
    body = (row.body or "").strip()
    if body:
        return True
    if row.have_attachment:
        return True
    return bool(getattr(row, "media_file", None) and row.media_file)


def upsert_channex_message_from_payload(
    *,
    tenant: Tenant,
    integration: IntegrationConfig,
    payload: dict[str, Any],
    reservation: Reservation | None = None,
) -> tuple[ChannexMessage, bool]:
    flat = _flatten_message_payload(payload)
    message_id = channex_message_id_from_payload(flat)
    existing = ChannexMessage.objects.filter(channex_message_id=message_id).first()
    if existing is not None:
        return existing, False

    booking_id = str(flat.get("booking_id") or "").strip()
    if reservation is None and booking_id:
        ota = str(
            flat.get("ota_reservation_code") or flat.get("ota_reservation_id") or ""
        ).strip()
        reservation = resolve_reservation_for_channex_message(
            tenant,
            booking_id=booking_id,
            ota_reservation_id=ota,
        )

    sender = _message_sender(flat)
    create_kwargs: dict[str, Any] = {
        "tenant": tenant,
        "integration": integration,
        "reservation": reservation,
        "channex_booking_id": booking_id,
        "message_thread_id": str(flat.get("message_thread_id") or "").strip(),
        "channex_message_id": message_id,
        "direction": _direction_for_sender(sender),
        "sender": sender,
        "body": _message_body(flat),
        "have_attachment": bool(flat.get("have_attachment")) or bool(_attachment_paths(flat)),
        "raw_payload": flat,
    }
    inserted_at = _message_created_at(flat)
    row = ChannexMessage.objects.create(**create_kwargs)
    if inserted_at is not None:
        ChannexMessage.objects.filter(pk=row.pk).update(created_at=inserted_at)
        row.created_at = inserted_at
    if reservation is not None and _channex_message_visible_in_timeline(row):
        touch_reservation_version(
            reservation.pk,
            ReservationVersionScope.MESSAGES,
            reason="channex_message",
        )
    if reservation is None and booking_id:
        logger.warning(
            "channex message stored without reservation link",
            extra={
                "tenant_slug": tenant.slug,
                "booking_id": booking_id,
                "message_id": message_id,
            },
        )
    return row, True


def _maybe_notify_channex_guest_message(row: ChannexMessage, *, created: bool) -> None:
    if (
        created
        and row.reservation_id
        and row.sender == ChannexMessage.Sender.GUEST
        and (row.body or "").strip()
    ):
        from apps.communications.guest_language_inbound import on_guest_inbound_message
        from apps.reservations.models import Reservation

        reservation = Reservation.objects.select_related("property", "tenant").get(
            pk=row.reservation_id,
        )
        on_guest_inbound_message(
            reservation,
            body=row.body or "",
            channel="booking",
            received_at=row.created_at,
        )

        from apps.core.tasks import notify_guest_message_inbound

        notify_guest_message_inbound.delay(
            row.reservation_id,
            channel="booking",
            body_preview=row.body or "",
        )


def process_channex_message_webhook(
    integration_row: IntegrationConfig,
    *,
    property_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise ChannexBookingIngestError("Channex message webhook missing payload.")

    tenant = integration_row.tenant
    booking_id = str(payload.get("booking_id") or "").strip()
    ota = str(
        payload.get("ota_reservation_code") or payload.get("ota_reservation_id") or ""
    ).strip()
    reservation = (
        resolve_reservation_for_channex_message(
            tenant,
            booking_id=booking_id,
            ota_reservation_id=ota,
        )
        if booking_id or ota
        else None
    )
    row, created = upsert_channex_message_from_payload(
        tenant=tenant,
        integration=integration_row,
        payload=payload,
        reservation=reservation,
    )
    ensure_channex_message_media(row)
    logger.info(
        "channex message webhook processed",
        extra={
            "tenant_slug": tenant.slug,
            "property_id": property_id,
            "booking_id": booking_id,
            "message_id": row.channex_message_id,
            "message_created": created,
            "reservation_id": row.reservation_id,
        },
    )
    _maybe_notify_channex_guest_message(row, created=created)
    if (
        created
        and row.reservation_id
        and row.sender == ChannexMessage.Sender.GUEST
        and (row.body or "").strip()
    ):
        from apps.communications.guest_arrival_inbound import maybe_handle_guest_arrival_inbound
        from apps.reservations.models import Reservation

        reservation = Reservation.objects.select_related("property", "tenant").get(
            pk=row.reservation_id,
        )
        arrival_result = maybe_handle_guest_arrival_inbound(
            reservation,
            row.body,
            channel="booking",
        )
        if arrival_result is None:
            from apps.communications.guest_invoice_inbound import (
                maybe_handle_guest_invoice_inbound,
            )

            invoice_result = maybe_handle_guest_invoice_inbound(
                reservation,
                row.body,
                channel="booking",
            )
            if invoice_result is None:
                from apps.communications.guest_parking_inbound import (
                    maybe_handle_guest_parking_inbound,
                )

                maybe_handle_guest_parking_inbound(
                    reservation,
                    row.body,
                    channel="booking",
                )
    return {
        "message_id": row.channex_message_id,
        "created": created,
        "reservation_id": row.reservation_id,
    }


def sync_booking_messages_from_channex(
    integration_row: IntegrationConfig,
    reservation: Reservation,
    *,
    client: ChannexClient | None = None,
) -> list[ChannexMessage]:
    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    booking_id = resolve_channex_booking_id_for_reservation(
        integration_row,
        reservation,
        client=client,
        repair_external_id=True,
    )
    if not booking_id:
        raise ChannexBookingIngestError("Reservation is not linked to a Channex booking.")

    stored: list[ChannexMessage] = []
    try:
        response = client.list_booking_messages(booking_id)
        for row_payload in _extract_message_rows(response):
            if not row_payload.get("booking_id"):
                row_payload = {**row_payload, "booking_id": booking_id}
            message, created = upsert_channex_message_from_payload(
                tenant=reservation.tenant,
                integration=integration_row,
                payload=row_payload,
                reservation=reservation,
            )
            ensure_channex_message_media(message, client=client)
            _maybe_notify_channex_guest_message(message, created=created)
            stored.append(message)
    finally:
        if owns_client and client is not None:
            client.close()
    return stored


def list_messages_for_reservation(
    integration_row: IntegrationConfig,
    reservation: Reservation,
    *,
    sync_if_empty: bool = False,
    force_sync: bool = False,
    client: ChannexClient | None = None,
) -> list[ChannexMessage]:
    """Return stored Channex rows. Does not call the provider unless explicitly asked.

    GET handlers must pass sync_if_empty=False and force_sync=False (ADR 0019).
    """
    qs = ChannexMessage.objects.filter(
        tenant=reservation.tenant,
        reservation=reservation,
    )
    if _reservation_can_sync_messages(reservation) and (
        force_sync or (sync_if_empty and not qs.exists())
    ):
        try:
            sync_booking_messages_from_channex(
                integration_row,
                reservation,
                client=client,
            )
        except ChannexBookingIngestError:
            logger.warning(
                "channex message sync skipped",
                extra={"reservation_id": reservation.pk},
            )
    return list(qs.order_by("-created_at"))


def send_message_for_reservation(
    integration_row: IntegrationConfig,
    reservation: Reservation,
    message: str,
    *,
    client: ChannexClient | None = None,
) -> ChannexMessage:
    text = (message or "").strip()
    if not text:
        raise ChannexBookingIngestError("Message body is required.")

    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    booking_id = resolve_channex_booking_id_for_reservation(
        integration_row,
        reservation,
        client=client,
        repair_external_id=True,
    )
    if not booking_id:
        raise ChannexBookingIngestError("Reservation is not linked to a Channex booking.")

    if reservation.import_source != "channex":
        raise ChannexBookingIngestError("Reservation was not imported from Channex.")

    try:
        response = client.send_booking_message(booking_id, text)
        rows = _extract_message_rows(response)
        payload: dict[str, Any]
        if rows:
            payload = rows[0]
        else:
            payload = {
                "id": f"local-outbound:{reservation.pk}:{ChannexMessage.objects.count() + 1}",
                "booking_id": booking_id,
                "sender": ChannexMessage.Sender.PROPERTY,
                "message": text,
            }
        if not payload.get("booking_id"):
            payload = {**payload, "booking_id": booking_id}
        if payload.get("sender") != ChannexMessage.Sender.PROPERTY:
            payload = {**payload, "sender": ChannexMessage.Sender.PROPERTY}
        row, _created = upsert_channex_message_from_payload(
            tenant=reservation.tenant,
            integration=integration_row,
            payload=payload,
            reservation=reservation,
        )
        return row
    except ChannexApiError:
        raise
    finally:
        if owns_client and client is not None:
            client.close()


def send_image_for_reservation(
    integration_row: IntegrationConfig,
    reservation: Reservation,
    *,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    caption: str = "",
    client: ChannexClient | None = None,
) -> ChannexMessage:
    if not file_bytes:
        raise ChannexBookingIngestError("Empty image file.")

    if reservation.import_source != "channex":
        raise ChannexBookingIngestError("Reservation was not imported from Channex.")

    if not mime_type.startswith("image/"):
        raise ChannexBookingIngestError("Unsupported media type.")

    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    booking_id = resolve_channex_booking_id_for_reservation(
        integration_row,
        reservation,
        client=client,
        repair_external_id=True,
    )
    if not booking_id:
        raise ChannexBookingIngestError("Reservation is not linked to a Channex booking.")

    text = (caption or "").strip()
    try:
        attachment_id = client.upload_attachment(
            file_bytes=file_bytes,
            file_name=filename,
            file_type=mime_type,
        )
        response = client.send_booking_message(
            booking_id,
            text,
            attachment_id=attachment_id,
        )
        rows = _extract_message_rows(response)
        payload: dict[str, Any]
        if rows:
            payload = rows[0]
        else:
            payload = {
                "id": f"local-outbound-image:{reservation.pk}:{ChannexMessage.objects.count() + 1}",
                "booking_id": booking_id,
                "sender": ChannexMessage.Sender.PROPERTY,
                "message": text,
                "have_attachment": True,
            }
        if not payload.get("booking_id"):
            payload = {**payload, "booking_id": booking_id}
        if payload.get("sender") != ChannexMessage.Sender.PROPERTY:
            payload = {**payload, "sender": ChannexMessage.Sender.PROPERTY}
        row, _created = upsert_channex_message_from_payload(
            tenant=reservation.tenant,
            integration=integration_row,
            payload=payload,
            reservation=reservation,
        )
        display_body = text or "📷 Slika poslana"
        row.body = display_body
        row.have_attachment = True
        row.media_file.save(filename, ContentFile(file_bytes), save=False)
        row.save(update_fields=["body", "have_attachment", "media_file"])
        return row
    except ChannexApiError:
        raise
    finally:
        if owns_client and client is not None:
            client.close()


def serialize_channex_message(row: ChannexMessage) -> dict[str, Any]:
    return {
        "id": row.pk,
        "channex_message_id": row.channex_message_id,
        "direction": row.direction,
        "sender": row.sender,
        "body": row.body,
        "have_attachment": row.have_attachment,
        "message_thread_id": row.message_thread_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
