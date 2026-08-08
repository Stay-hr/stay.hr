from __future__ import annotations

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from apps.tenants.login_audit import record_staff_login_event
from apps.tenants.models import StaffLoginEvent


@receiver(user_logged_in)
def record_admin_staff_login(sender, request, user, **kwargs):
    if request is None or not user.is_staff:
        return

    path = getattr(request, "path", "") or ""
    if "/auth/reception-login" in path:
        return
    if not path.startswith("/admin/"):
        return

    record_staff_login_event(
        user=user,
        username=user.username,
        channel=StaffLoginEvent.Channel.ADMIN,
        tenant=getattr(request, "tenant", None),
        request=request,
    )
