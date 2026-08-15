from celery import shared_task

# Register WhatsApp autocheck-in beat tasks (module is not named tasks.py).
from apps.communications import whatsapp_autocheckin_tasks  # noqa: F401

# Late-booking Channex check-in link (create enqueue).
from apps.communications import guest_checkin_channex  # noqa: F401

# ADR 0010 Phase 6 — orchestration beat entry (MESSAGE_ORCHESTRATION_* gated).
from apps.communications.messaging.tasks import run_message_orchestration  # noqa: F401


@shared_task
def send_guest_booking_confirmed_email(reservation_id: int) -> dict:
    from apps.communications.guest_email import send_booking_confirmed_email

    return send_booking_confirmed_email(reservation_id)


@shared_task
def send_guest_booking_refused_email(reservation_id: int, reason: str = "") -> dict:
    from apps.communications.guest_email import send_booking_refused_email

    return send_booking_refused_email(reservation_id, reason=reason)


@shared_task
def send_guest_booking_canceled_email(reservation_id: int) -> dict:
    from apps.communications.guest_email import send_booking_canceled_email

    return send_booking_canceled_email(reservation_id)
