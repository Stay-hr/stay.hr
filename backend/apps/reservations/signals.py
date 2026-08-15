from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.reservations.models import Reservation, ReservationUnit


@receiver(post_save, sender=Reservation)
def reservation_created_notify(sender, instance: Reservation, created: bool, **kwargs):
    if not created:
        return
    if instance.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        return

    from apps.reservations.booking_lifecycle import is_web_pending_booking

    # Web bookings: push after channel manager confirms (see booking_lifecycle.confirm_web_booking).
    if is_web_pending_booking(instance):
        return

    from apps.core.tasks import notify_new_reservation

    notify_new_reservation.delay(instance.pk)


@receiver(post_save, sender=Reservation)
def reservation_maybe_immediate_autocheckin_welcome(
    sender,
    instance: Reservation,
    created: bool,
    **kwargs,
):
    if not created:
        return
    if kwargs.get("raw"):
        return
    if instance.status in {
        Reservation.Status.CANCELED,
        Reservation.Status.NO_SHOW,
    }:
        return

    from apps.reservations.booking_lifecycle import is_web_pending_booking

    if is_web_pending_booking(instance):
        return

    from apps.communications.whatsapp_autocheckin_tasks import (
        maybe_send_immediate_autocheckin_welcome,
    )

    transaction.on_commit(
        lambda reservation_id=instance.pk: maybe_send_immediate_autocheckin_welcome.delay(
            reservation_id
        )
    )


@receiver(post_save, sender=Reservation)
def reservation_maybe_immediate_channex_checkin_link(
    sender,
    instance: Reservation,
    created: bool,
    **kwargs,
):
    """Enqueue late-booking Channex check-in link; task re-validates authoritatively."""
    if not created:
        return
    if kwargs.get("raw"):
        return
    if instance.status in {
        Reservation.Status.CANCELED,
        Reservation.Status.NO_SHOW,
    }:
        return

    from apps.reservations.booking_lifecycle import is_web_pending_booking

    if is_web_pending_booking(instance):
        return

    # Cheap prefilter only — task reloads and rechecks window/status/channex.
    if instance.import_source != "channex":
        return

    from apps.communications.guest_checkin_channex import (
        maybe_send_immediate_channex_checkin_link,
    )

    transaction.on_commit(
        lambda reservation_id=instance.pk: maybe_send_immediate_channex_checkin_link.delay(
            reservation_id
        )
    )


@receiver(post_save, sender=Reservation)
def reservation_outbound_on_create(sender, instance: Reservation, created: bool, **kwargs):
    if not created:
        return
    if instance.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        return

    from apps.integrations.channel_manager.tasks import sync_reservation_outbound_task

    transaction.on_commit(
        lambda reservation_id=instance.pk: sync_reservation_outbound_task.delay(
            reservation_id,
            "sync",
        )
    )


@receiver(pre_save, sender=Reservation)
def reservation_snapshot_before_save(sender, instance: Reservation, **kwargs):
    if not instance.pk:
        instance._channel_sync_snapshot = None
        return
    try:
        old = Reservation.objects.get(pk=instance.pk)
    except Reservation.DoesNotExist:
        instance._channel_sync_snapshot = None
        return
    instance._channel_sync_snapshot = {
        "check_in": old.check_in,
        "check_out": old.check_out,
        "status": old.status,
    }


@receiver(post_save, sender=Reservation)
def reservation_outbound_on_update(sender, instance: Reservation, created: bool, **kwargs):
    if created:
        return

    snapshot = getattr(instance, "_channel_sync_snapshot", None)
    if not snapshot:
        return

    from apps.integrations.channel_manager.tasks import sync_reservation_outbound_task

    old_check_in = snapshot["check_in"]
    old_check_out = snapshot["check_out"]
    old_status = snapshot["status"]

    dates_changed = old_check_in != instance.check_in or old_check_out != instance.check_out
    status_changed = old_status != instance.status

    if not dates_changed and not status_changed:
        return

    def queue_updates():
        if instance.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
            sync_reservation_outbound_task.delay(instance.pk, "remove")
            return

        if status_changed and instance.status in {
            Reservation.Status.PENDING,
            Reservation.Status.EXPECTED,
            Reservation.Status.CHECKED_IN,
        }:
            sync_reservation_outbound_task.delay(instance.pk, "sync")
            return

        if dates_changed:
            from apps.integrations.channel_manager.resolver import get_channel_manager
            from apps.integrations.channex.reservation_availability_service import (
                sync_reservation_channex_availability_for_dates,
            )
            from apps.tenants.models import ChannelManager

            if get_channel_manager(instance.tenant) == ChannelManager.CHANNEX:
                sync_reservation_channex_availability_for_dates(
                    instance, old_check_in, old_check_out
                )
            sync_reservation_outbound_task.delay(instance.pk, "sync")

    transaction.on_commit(queue_updates)


def _queue_availability_if_reservation_active(reservation_id: int) -> None:
    reservation = Reservation.objects.filter(pk=reservation_id).only("status").first()
    if reservation is None:
        return
    if reservation.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        from apps.integrations.channel_manager.tasks import sync_reservation_outbound_task

        transaction.on_commit(
            lambda rid=reservation_id: sync_reservation_outbound_task.delay(rid, "remove")
        )
        return

    from apps.reservations.channel_availability_sync import (
        queue_reservation_channel_availability_sync,
    )

    queue_reservation_channel_availability_sync(reservation_id)


@receiver(post_save, sender=ReservationUnit)
def reservation_unit_outbound_on_save(
    sender,
    instance: ReservationUnit,
    created: bool,
    **kwargs,
):
    if kwargs.get("raw"):
        return
    from apps.reservations.channel_availability_sync import unit_availability_sync_suppressed

    if unit_availability_sync_suppressed():
        return
    _queue_availability_if_reservation_active(instance.reservation_id)


@receiver(post_delete, sender=ReservationUnit)
def reservation_unit_outbound_on_delete(sender, instance: ReservationUnit, **kwargs):
    from apps.reservations.channel_availability_sync import unit_availability_sync_suppressed

    if unit_availability_sync_suppressed():
        return
    _queue_availability_if_reservation_active(instance.reservation_id)
