from __future__ import annotations

import ast
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db.models import Q, QuerySet, TextField, Value
from django.db.models.functions import Coalesce, Trim
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai.provider import GuestComposeError, complete_chat, llm_configured
from apps.ai.translate import translation_available, translate_text
from apps.api.language import normalize_app_language
from apps.communications.guest_compose_language import SUPPORTED_COMPOSE_LANGS
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.channex.message_service import find_reservation_for_channex_booking
from apps.integrations.channex.review_reply_policy import (
    booking_compliant_fallback,
    hint_is_compliant,
    validate_review_reply,
)
from apps.integrations.models import ChannexReview, IntegrationConfig
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

AIRBNB_OTA = "AirBNB"
BOOKING_COM_OTA = "BookingCom"
REPLY_BLOCK_REPLIED = "replied"
REPLY_BLOCK_EXPIRED = "expired"
REPLY_BLOCK_RATING_ONLY = "rating_only"
REPLY_BLOCK_AIRBNB_HIDDEN = "airbnb_hidden"
REPLY_BLOCK_MESSAGES = {
    REPLY_BLOCK_REPLIED: "This review already has a published reply.",
    REPLY_BLOCK_EXPIRED: "The reply window for this review has expired.",
    REPLY_BLOCK_RATING_ONLY: "Booking.com does not allow replies to rating-only reviews.",
    REPLY_BLOCK_AIRBNB_HIDDEN: (
        "Airbnb hidden reviews cannot be replied to until you rate the guest."
    ),
}
DEFAULT_PAGE_SIZE = 25
REVIEWS_SYNC_CACHE_PREFIX = "channex-reviews-sync"
REVIEWS_SYNC_INTERVAL_SECONDS = 6 * 3600
RECENT_CHECKOUT_REVIEW_SYNC_DAYS = 7


def channex_review_id_from_payload(payload: dict[str, Any]) -> str:
    for key in ("id", "channex_review_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    raise ChannexBookingIngestError("Channex review payload missing id.")


def _flatten_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    attrs = payload.get("attributes")
    if isinstance(attrs, dict):
        merged = dict(attrs)
        if payload.get("id") and not merged.get("id"):
            merged["id"] = payload["id"]
        booking = (payload.get("relationships") or {}).get("booking") or {}
        booking_data = booking.get("data") if isinstance(booking, dict) else None
        if isinstance(booking_data, dict) and booking_data.get("id"):
            merged.setdefault("booking_id", str(booking_data["id"]))
        return merged
    return payload


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    parsed = parse_datetime(str(raw).replace("Z", "+00:00"))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _parse_score(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _guest_name(payload: dict[str, Any]) -> str:
    return str(
        payload.get("guest_name")
        or payload.get("reviewer_name")
        or ""
    ).strip()


def _normalize_reply_text(raw: Any) -> str:
    """Extract plain reply text from Channex payloads (dict or legacy str(dict))."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        inner = raw.get("reply")
        if isinstance(inner, str):
            return inner.strip()
        if inner is not None:
            return str(inner).strip()
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if text.startswith("{") and "reply" in text:
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(parsed, dict):
            return _normalize_reply_text(parsed.get("reply"))
    return text


def resolve_reservation_for_review(
    tenant: Tenant,
    *,
    booking_id: str = "",
    ota_reservation_id: str = "",
) -> Reservation | None:
    """Match a Channex review to a stay.hr reservation."""
    booking_id = (booking_id or "").strip()
    ota_reservation_id = (ota_reservation_id or "").strip()

    if booking_id:
        reservation = find_reservation_for_channex_booking(tenant, booking_id)
        if reservation is not None:
            return reservation

    if not ota_reservation_id:
        return None

    reservation = (
        Reservation.objects.filter(
            tenant=tenant,
            booking_code=ota_reservation_id,
        )
        .first()
    )
    if reservation is not None:
        return reservation

    return Reservation.objects.filter(
        tenant=tenant,
        external_id=ota_reservation_id,
    ).first()


def relink_unlinked_channex_reviews(tenant: Tenant) -> int:
    """Retry reservation matching for reviews stored without a link."""
    updated = 0
    qs = ChannexReview.objects.filter(tenant=tenant, reservation__isnull=True)
    for row in qs.iterator():
        reservation = resolve_reservation_for_review(
            tenant,
            booking_id=row.channex_booking_id or "",
            ota_reservation_id=row.ota_reservation_id or "",
        )
        if reservation is None:
            continue
        row.reservation = reservation
        row.save(update_fields=["reservation", "updated_at"])
        updated += 1
    return updated


def repair_channex_review_replies(tenant: Tenant | None = None) -> int:
    """Normalize stored reply text and infer is_replied for legacy rows."""
    qs = ChannexReview.objects.all()
    if tenant is not None:
        qs = qs.filter(tenant=tenant)

    updated = 0
    for row in qs.iterator():
        normalized = _normalize_reply_text(row.reply)
        update_fields: list[str] = []
        if normalized != (row.reply or ""):
            row.reply = normalized
            update_fields.append("reply")
        if normalized and not row.is_replied:
            row.is_replied = True
            update_fields.append("is_replied")
        if normalized and row.reply_sent_at is None and row.ota != BOOKING_COM_OTA:
            row.reply_sent_at = row.updated_at or timezone.now()
            update_fields.append("reply_sent_at")
        if update_fields:
            update_fields.append("updated_at")
            row.save(update_fields=update_fields)
            updated += 1
    return updated


def _review_fields_from_payload(
    flat: dict[str, Any],
    *,
    reservation: Reservation | None,
) -> dict[str, Any]:
    booking_id = str(flat.get("booking_id") or "").strip()
    reply_text = _normalize_reply_text(flat.get("reply"))
    is_replied = bool(flat.get("is_replied"))
    if reply_text and not is_replied:
        is_replied = True
    return {
        "channex_booking_id": booking_id,
        "reservation": reservation,
        "ota": str(flat.get("ota") or "").strip(),
        "ota_reservation_id": str(flat.get("ota_reservation_id") or "").strip(),
        "ota_review_id": str(flat.get("ota_review_id") or "").strip(),
        "guest_name": _guest_name(flat),
        "content": str(flat.get("content") or flat.get("raw_content") or "").strip(),
        "reply": reply_text,
        "overall_score": _parse_score(flat.get("overall_score") or flat.get("ota_overall_score")),
        "scores": flat.get("scores") if isinstance(flat.get("scores"), list) else [],
        "tags": flat.get("tags") if isinstance(flat.get("tags"), list) else [],
        "is_replied": is_replied,
        "is_hidden": bool(flat.get("is_hidden")),
        "expired_at": _parse_dt(flat.get("expired_at")),
        "received_at": _parse_dt(flat.get("received_at") or flat.get("inserted_at")),
        "reply_sent_at": _parse_dt(flat.get("reply_sent_at")),
        "reply_scheduled_at": _parse_dt(flat.get("reply_scheduled_at")),
        "raw_payload": flat,
    }


def _extract_review_rows(api_response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in api_response.get("data") or []:
        if isinstance(item, dict):
            rows.append(_flatten_review_payload(item))
    data = api_response.get("data")
    if isinstance(data, dict):
        rows.append(_flatten_review_payload(data))
    return rows


def upsert_channex_review_from_payload(
    *,
    tenant: Tenant,
    integration: IntegrationConfig,
    payload: dict[str, Any],
    reservation: Reservation | None = None,
) -> tuple[ChannexReview, bool, bool]:
    """Return (row, created, content_just_arrived)."""
    flat = _flatten_review_payload(payload)
    review_id = channex_review_id_from_payload(flat)
    booking_id = str(flat.get("booking_id") or "").strip()
    ota_reservation_id = str(flat.get("ota_reservation_id") or "").strip()
    if reservation is None:
        reservation = resolve_reservation_for_review(
            tenant,
            booking_id=booking_id,
            ota_reservation_id=ota_reservation_id,
        )

    field_values = _review_fields_from_payload(flat, reservation=reservation)
    existing = ChannexReview.objects.filter(channex_review_id=review_id).first()
    if existing is None:
        row = ChannexReview.objects.create(
            tenant=tenant,
            integration=integration,
            channex_review_id=review_id,
            **field_values,
        )
        if reservation is None and (booking_id or ota_reservation_id):
            logger.warning(
                "channex review stored without reservation link",
                extra={
                    "tenant_slug": tenant.slug,
                    "booking_id": booking_id,
                    "ota_reservation_id": ota_reservation_id,
                    "review_id": review_id,
                },
            )
        return row, True, bool(field_values["content"])

    had_content = bool((existing.content or "").strip())
    new_content = field_values["content"]
    if had_content and not (new_content or "").strip():
        field_values["content"] = existing.content
        new_content = existing.content
    content_just_arrived = bool(new_content) and not had_content
    content_changed = new_content != (existing.content or "")

    for key, value in field_values.items():
        setattr(existing, key, value)
    if content_changed:
        existing.content_translations = {}
    existing.integration = integration
    existing.save()
    return existing, False, content_just_arrived


def _should_notify_review(
    *,
    created: bool,
    content_just_arrived: bool,
    event: str,
    row: ChannexReview,
) -> bool:
    if not row.reservation_id or row.is_replied:
        return False
    if created and event == "review":
        return True
    if event == "updated_review" and content_just_arrived:
        return True
    return False


def process_channex_review_webhook(
    integration_row: IntegrationConfig,
    *,
    property_id: str,
    body: dict[str, Any],
    event: str,
) -> dict[str, Any]:
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise ChannexBookingIngestError("Channex review webhook missing payload.")

    tenant = integration_row.tenant
    booking_id = str(payload.get("booking_id") or "").strip()
    ota_reservation_id = str(payload.get("ota_reservation_id") or "").strip()
    reservation = resolve_reservation_for_review(
        tenant,
        booking_id=booking_id,
        ota_reservation_id=ota_reservation_id,
    )
    row, created, content_just_arrived = upsert_channex_review_from_payload(
        tenant=tenant,
        integration=integration_row,
        payload=payload,
        reservation=reservation,
    )
    logger.info(
        "channex review webhook processed",
        extra={
            "tenant_slug": tenant.slug,
            "property_id": property_id,
            "event": event,
            "booking_id": booking_id,
            "review_id": row.channex_review_id,
            "review_created": created,
            "reservation_id": row.reservation_id,
        },
    )
    if _should_notify_review(
        created=created,
        content_just_arrived=content_just_arrived,
        event=event,
        row=row,
    ):
        from apps.core.tasks import notify_guest_review_inbound

        notify_guest_review_inbound.delay(
            row.reservation_id,
            review_id=row.pk,
            ota=row.ota or "",
            score_preview=str(row.overall_score) if row.overall_score is not None else "",
            content_preview=(row.content or "")[:200],
        )
    return {
        "review_id": row.channex_review_id,
        "created": created,
        "reservation_id": row.reservation_id,
    }


def sync_reviews_from_channex(
    integration_row: IntegrationConfig,
    *,
    client: ChannexClient | None = None,
    max_pages: int = 5,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[ChannexReview]:
    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    if not config.property_id:
        raise ChannexBookingIngestError("Channex property_id is not configured.")

    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    stored: list[ChannexReview] = []
    try:
        for page in range(1, max_pages + 1):
            response = client.list_reviews(
                params={
                    "filter[property_id]": config.property_id,
                    "pagination[page]": page,
                    "pagination[limit]": page_size,
                }
            )
            rows = _extract_review_rows(response)
            if not rows:
                break
            for row_payload in rows:
                review, _created, _content = upsert_channex_review_from_payload(
                    tenant=integration_row.tenant,
                    integration=integration_row,
                    payload=row_payload,
                )
                stored.append(review)
            meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
            total_pages = int(meta.get("total") or 0)
            if page * page_size >= total_pages and total_pages > 0:
                break
            if len(rows) < page_size:
                break
    finally:
        if owns_client and client is not None:
            client.close()
    relink_unlinked_channex_reviews(integration_row.tenant)
    mark_reviews_synced(integration_row.tenant_id)
    return stored


def _reviews_sync_cache_key(tenant_id: int) -> str:
    return f"{REVIEWS_SYNC_CACHE_PREFIX}:{tenant_id}"


def reviews_sync_is_stale(tenant_id: int) -> bool:
    last_sync = cache.get(_reviews_sync_cache_key(tenant_id))
    if last_sync is None:
        return True
    try:
        parsed = datetime.fromisoformat(str(last_sync))
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        elapsed = (timezone.now() - parsed).total_seconds()
    except (TypeError, ValueError):
        return True
    return elapsed >= REVIEWS_SYNC_INTERVAL_SECONDS


def mark_reviews_synced(tenant_id: int) -> None:
    cache.set(
        _reviews_sync_cache_key(tenant_id),
        timezone.now().isoformat(),
        timeout=REVIEWS_SYNC_INTERVAL_SECONDS * 4,
    )


def reservation_should_auto_sync_reviews(reservation: Reservation) -> bool:
    """Recently checked-out Channex reservations may get reviews with a delay."""
    if reservation.status != Reservation.Status.CHECKED_OUT:
        return False
    if not reservation.check_out:
        return False
    cutoff = timezone.localdate() - timedelta(days=RECENT_CHECKOUT_REVIEW_SYNC_DAYS)
    return reservation.check_out >= cutoff


def _should_pull_reviews_from_channex(
    *,
    tenant_id: int,
    force_sync: bool,
    sync_if_empty: bool,
    sync_if_stale: bool,
    queryset: QuerySet[ChannexReview],
) -> bool:
    if force_sync:
        return True
    if sync_if_empty and not queryset.exists():
        return True
    if sync_if_stale and reviews_sync_is_stale(tenant_id):
        return True
    return False


def _pull_reviews_from_channex(
    integration_row: IntegrationConfig,
    *,
    client: ChannexClient | None = None,
    max_pages: int = 10,
) -> None:
    sync_reviews_from_channex(integration_row, client=client, max_pages=max_pages)


def _reviews_queryset(tenant: Tenant) -> QuerySet[ChannexReview]:
    return ChannexReview.objects.filter(tenant=tenant).select_related("reservation")


def list_reviews_for_property(
    integration_row: IntegrationConfig,
    *,
    unreplied_only: bool = False,
    ota: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sync_if_empty: bool = True,
    sync_if_stale: bool = False,
    force_sync: bool = False,
    client: ChannexClient | None = None,
) -> tuple[list[ChannexReview], int]:
    tenant = integration_row.tenant
    relink_unlinked_channex_reviews(tenant)
    qs = _reviews_queryset(tenant)

    if _should_pull_reviews_from_channex(
        tenant_id=tenant.pk,
        force_sync=force_sync,
        sync_if_empty=sync_if_empty,
        sync_if_stale=sync_if_stale,
        queryset=qs,
    ):
        _pull_reviews_from_channex(integration_row, client=client)
        qs = _reviews_queryset(tenant)

    if unreplied_only:
        qs = filter_reply_actionable(qs)
    ota_filter = (ota or "").strip()
    if ota_filter:
        qs = qs.filter(ota__iexact=ota_filter)

    total = qs.count()
    offset = max(page - 1, 0) * page_size
    rows = list(qs.order_by("-received_at", "-created_at")[offset : offset + page_size])
    return rows, total


def _reviews_queryset_for_reservation(reservation: Reservation) -> QuerySet[ChannexReview]:
    booking_ids = _booking_ids_for_reservation(reservation)
    filters = Q(reservation=reservation)
    if booking_ids:
        filters |= Q(channex_booking_id__in=booking_ids)
    ota_code = (reservation.booking_code or "").strip()
    if ota_code:
        filters |= Q(ota_reservation_id=ota_code)
    return ChannexReview.objects.filter(tenant=reservation.tenant).filter(filters)


def list_reviews_for_reservation(
    integration_row: IntegrationConfig,
    reservation: Reservation,
    *,
    sync_if_empty: bool = False,
    sync_if_stale: bool = False,
    force_sync: bool = False,
    client: ChannexClient | None = None,
) -> list[ChannexReview]:
    relink_unlinked_channex_reviews(reservation.tenant)
    qs = _reviews_queryset_for_reservation(reservation)
    if force_sync or sync_if_stale or (sync_if_empty and not qs.exists()):
        _pull_reviews_from_channex(integration_row, client=client)
        relink_unlinked_channex_reviews(reservation.tenant)
        qs = _reviews_queryset_for_reservation(reservation)
    return list(qs.order_by("-received_at", "-created_at"))


def _booking_ids_for_reservation(reservation: Reservation) -> list[str]:
    from apps.integrations.channex.booking_service import parse_channex_booking_id

    booking_id = parse_channex_booking_id(reservation.external_id)
    return [booking_id] if booking_id else []


def get_review_for_tenant(tenant: Tenant, review_pk: int) -> ChannexReview | None:
    relink_unlinked_channex_reviews(tenant)
    return (
        _reviews_queryset(tenant)
        .filter(pk=review_pk)
        .first()
    )


def review_guest_review_allowed(row: ChannexReview) -> bool:
    return row.ota == AIRBNB_OTA and row.is_hidden


def reply_published(row: ChannexReview) -> bool:
    return row.reply_sent_at is not None


def reply_pending_moderation(row: ChannexReview) -> bool:
    """Submitted to OTA channel but publication not confirmed (Booking.com heuristic)."""
    if row.ota != BOOKING_COM_OTA:
        return False
    if reply_published(row):
        return False
    return bool(_normalize_reply_text(row.reply))


def review_reply_block_reason(row: ChannexReview) -> str | None:
    """Closed enum of why a review cannot be replied to, or None if actionable."""
    if reply_published(row):
        return REPLY_BLOCK_REPLIED
    if row.expired_at and row.expired_at <= timezone.now():
        return REPLY_BLOCK_EXPIRED
    if row.ota == AIRBNB_OTA and row.is_hidden:
        return REPLY_BLOCK_AIRBNB_HIDDEN
    if row.ota == BOOKING_COM_OTA and not (row.content or "").strip():
        return REPLY_BLOCK_RATING_ONLY
    return None


def review_reply_allowed(row: ChannexReview) -> bool:
    return review_reply_block_reason(row) is None


def review_reply_block_message(row: ChannexReview) -> str | None:
    reason = review_reply_block_reason(row)
    if reason is None:
        return None
    return REPLY_BLOCK_MESSAGES[reason]


def filter_reply_actionable(qs: QuerySet[ChannexReview]) -> QuerySet[ChannexReview]:
    """Queryset mirror of ``review_reply_block_reason(row) is None``.

    ``unreplied`` means not answered AND currently actionable. Keep this
    definition in lockstep with ``review_reply_block_reason``.
    """
    now = timezone.now()
    return (
        qs.annotate(
            _reply_actionable_content=Coalesce(
                Trim("content"),
                Value("", output_field=TextField()),
                output_field=TextField(),
            ),
        )
        .exclude(reply_sent_at__isnull=False)
        .exclude(expired_at__isnull=False, expired_at__lte=now)
        .exclude(ota=AIRBNB_OTA, is_hidden=True)
        .exclude(ota=BOOKING_COM_OTA, _reply_actionable_content="")
    )


def detect_review_language(content: str) -> str:
    """Best-effort language for OTA review reply (same as guest text when possible)."""
    text = (content or "").lower()
    if not text.strip():
        return "en"

    sk_markers = (
        "ľ",
        "ä",
        "ô",
        "ŕ",
        "veľk",
        "izba",
        "záchod",
        "ďakuj",
        "špinav",
        "kúpeľ",
        "ste ",
        "sme ",
        "prie",
    )
    if any(marker in text for marker in sk_markers):
        return "sk"

    de_markers = ("ß", "schön", "danke", "zimmer", "übernacht", "gäste")
    if any(marker in text for marker in de_markers):
        return "de"

    hr_markers = ("hvala", "soba", "boravak", "gost", "žao", "čist")
    if any(marker in text for marker in hr_markers):
        return "hr"

    return "en"


def reply_to_review(
    integration_row: IntegrationConfig,
    row: ChannexReview,
    reply_text: str,
    *,
    client: ChannexClient | None = None,
) -> ChannexReview:
    text = (reply_text or "").strip()
    if not text:
        raise ChannexBookingIngestError("Reply text is required.")
    blocked = review_reply_block_message(row)
    if blocked:
        raise ChannexBookingIngestError(blocked)

    errors = validate_review_reply(
        text,
        ota=row.ota or "",
        guest_name=row.guest_name or "",
        review_content=row.content or "",
    )
    if errors:
        raise ChannexBookingIngestError(errors[0])

    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    try:
        response = client.reply_to_review(row.channex_review_id, text)
        rows = _extract_review_rows(response)
        if rows:
            row, _created, _content = upsert_channex_review_from_payload(
                tenant=row.tenant,
                integration=integration_row,
                payload=rows[0],
                reservation=row.reservation,
            )
        normalized = _normalize_reply_text(row.reply) or text
        update_fields: list[str] = []
        if row.reply != normalized:
            row.reply = normalized
            update_fields.append("reply")
        if not row.is_replied:
            row.is_replied = True
            update_fields.append("is_replied")
        if update_fields:
            update_fields.append("updated_at")
            row.save(update_fields=update_fields)
        return row
    except ChannexApiError:
        raise
    finally:
        if owns_client and client is not None:
            client.close()


def submit_airbnb_guest_review(
    integration_row: IntegrationConfig,
    row: ChannexReview,
    *,
    scores: list[dict[str, Any]],
    public_review: str = "",
    private_review: str = "",
    is_reviewee_recommended: bool = True,
    tags: list[str] | None = None,
    client: ChannexClient | None = None,
) -> ChannexReview:
    if row.ota != AIRBNB_OTA:
        raise ChannexBookingIngestError("Guest review is only supported for Airbnb reviews.")
    if not scores:
        raise ChannexBookingIngestError("At least one score category is required.")

    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    payload = {
        "review": {
            "scores": scores,
            "public_review": (public_review or "").strip(),
            "private_review": (private_review or "").strip(),
            "is_reviewee_recommended": is_reviewee_recommended,
            "tags": tags or [],
        }
    }

    try:
        response = client.submit_guest_review(row.channex_review_id, payload)
        rows = _extract_review_rows(response)
        if rows:
            row, _created, _content = upsert_channex_review_from_payload(
                tenant=row.tenant,
                integration=integration_row,
                payload=rows[0],
                reservation=row.reservation,
            )
        return row
    except ChannexApiError:
        raise
    finally:
        if owns_client and client is not None:
            client.close()


def _cached_localized_content(row: ChannexReview, lang: str) -> tuple[str, bool] | None:
    target = lang.split("-")[0].lower()
    cache = row.content_translations if isinstance(row.content_translations, dict) else {}
    cached = cache.get(target)
    if not isinstance(cached, str) or not cached.strip():
        return None
    original = row.content or ""
    return cached, cached.strip() != original.strip()


def _localized_review_content(row: ChannexReview, lang: str) -> tuple[str, bool]:
    """Return (display_text, is_translated) for the requested UI language."""
    original = row.content or ""
    if not original.strip():
        return original, False

    target = lang.split("-")[0].lower()
    cache = row.content_translations if isinstance(row.content_translations, dict) else {}
    cached = cache.get(target)
    if isinstance(cached, str) and cached.strip():
        return cached, cached.strip() != original.strip()

    translated = translate_text(original, target)
    is_translated = translated.strip() != original.strip()
    if translated.strip():
        cache = dict(cache)
        cache[target] = translated
        row.content_translations = cache
        row.save(update_fields=["content_translations", "updated_at"])
    return translated, is_translated


def _property_display_name(row: ChannexReview) -> str:
    reservation = row.reservation
    if reservation is None:
        return ""
    prop = getattr(reservation, "property", None)
    if prop is not None and getattr(prop, "name", ""):
        return str(prop.name).strip()
    return ""


def compose_review_reply(
    row: ChannexReview,
    *,
    hint: str = "",
    language: str | None = None,
) -> tuple[str, bool, str]:
    """Return (body_text, llm_used, language) for a public OTA review reply draft."""
    blocked = review_reply_block_message(row)
    if blocked:
        raise ChannexBookingIngestError(blocked)

    original = (row.content or "").strip()
    if language:
        reply_lang = normalize_app_language(language)
        if reply_lang not in SUPPORTED_COMPOSE_LANGS and language.split("-")[0].lower() not in SUPPORTED_COMPOSE_LANGS:
            reply_lang = language.split("-")[0].lower()
    elif original:
        reply_lang = detect_review_language(original)
    else:
        reply_lang = "en"

    property_name = _property_display_name(row)
    scores_text = ""
    if isinstance(row.scores, list) and row.scores:
        parts = []
        for item in row.scores:
            if isinstance(item, dict):
                category = item.get("category") or item.get("name") or ""
                score = item.get("score") or item.get("rating")
                if category:
                    parts.append(f"{category}: {score}")
        scores_text = "; ".join(parts)

    system = (
        "You write professional, warm public replies to OTA guest reviews for a small hotel. "
        "For Booking.com: replies are moderated before publication (up to 72 hours). "
        "Write in the guest review language or English. "
        "Keep it concise (2–4 sentences, under 500 characters), thank the guest, acknowledge feedback professionally. "
        "NEVER include the guest's name, booking number, room number, dates, contact details, URLs, or phone numbers. "
        "Do NOT repeat explicit negative details from the review (e.g. dirty bathroom, insects, noise specifics). "
        "Do not offer compensation, contact details, links, or off-platform communication. "
        "No markdown, no placeholder brackets."
    )
    user_parts = [
        f"OTA: {row.ota or 'unknown'}",
        f"Guest review: {original or '(no text yet)'}",
    ]
    if row.overall_score is not None:
        user_parts.append(f"Overall score: {row.overall_score}/10")
    if scores_text:
        user_parts.append(f"Category scores: {scores_text}")
    if property_name:
        user_parts.append(f"Property: {property_name}")
    if row.ota != BOOKING_COM_OTA and row.guest_name:
        user_parts.append(f"Guest name: {row.guest_name}")
    staff_hint = (hint or "").strip()
    if staff_hint and hint_is_compliant(staff_hint):
        user_parts.append(f"Staff hint: {staff_hint}")
    user_parts.append(f"Write the reply in language code: {reply_lang}")

    fallback = booking_compliant_fallback(reply_lang)

    if not llm_configured():
        return fallback, False, reply_lang

    try:
        body = complete_chat(system, "\n".join(user_parts)).strip()
    except GuestComposeError:
        return fallback, False, reply_lang

    body = (body or fallback).strip()
    if row.ota == BOOKING_COM_OTA:
        errors = validate_review_reply(
            body,
            ota=row.ota,
            guest_name=row.guest_name or "",
            review_content=original,
        )
        if errors:
            body = booking_compliant_fallback(reply_lang)

    return body, True, reply_lang


def serialize_channex_review(
    row: ChannexReview,
    *,
    lang: str | None = None,
    translate: bool = False,
) -> dict[str, Any]:
    reservation = row.reservation
    booking_code = None
    if reservation is not None:
        booking_code = reservation.booking_code or str(reservation.pk)

    reservation_ref = booking_code or (row.ota_reservation_id or None)

    content_localized = row.content
    content_is_translated = False
    display_language = lang
    if lang and (row.content or "").strip():
        cached = _cached_localized_content(row, lang)
        if cached is not None:
            content_localized, content_is_translated = cached
        elif translate:
            content_localized, content_is_translated = _localized_review_content(row, lang)

    reply_text = _normalize_reply_text(row.reply) or None
    is_replied = row.is_replied or bool(reply_text)
    published = reply_published(row)
    pending = reply_pending_moderation(row)

    return {
        "id": row.pk,
        "channex_review_id": row.channex_review_id,
        "reservation_id": row.reservation_id,
        "booking_code": booking_code,
        "reservation_ref": reservation_ref,
        "reservation_linkable": row.reservation_id is not None,
        "ota": row.ota,
        "ota_reservation_id": row.ota_reservation_id,
        "guest_name": row.guest_name,
        "overall_score": float(row.overall_score) if row.overall_score is not None else None,
        "scores": row.scores,
        "tags": row.tags,
        "content": row.content,
        "content_localized": content_localized,
        "content_is_translated": content_is_translated,
        "display_language": display_language,
        "translation_available": translation_available(),
        "suggested_reply_language": detect_review_language(row.content or ""),
        "reply": reply_text,
        "is_replied": is_replied,
        "reply_published": published,
        "reply_pending_moderation": pending,
        "is_hidden": row.is_hidden,
        "expired_at": row.expired_at.isoformat() if row.expired_at else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "reply_sent_at": row.reply_sent_at.isoformat() if row.reply_sent_at else None,
        "can_reply": review_reply_allowed(row),
        "reply_blocked_reason": review_reply_block_reason(row),
        "can_submit_guest_review": review_guest_review_allowed(row),
    }
