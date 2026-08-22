from django.urls import include, path

from apps.api.auth_views import (
    ReceptionLoginView,
    ReceptionLogoutView,
    ReceptionSessionView,
)
from apps.api.billing_views import PublicInvoiceHtmlView, PublicInvoicePdfView
from apps.api.views import (
    AppConfigView,
    PublicAvailabilityView,
    PublicPropertiesView,
    PublicReservationCreateView,
    PublicReservationStatusView,
    PublicUnitsView,
)
from apps.api.site_context_views import SiteContextView
from apps.api.fcm_views import FcmTokenRegisterView
from apps.api.guest_checkin_views import (
    GuestCheckInCompleteView,
    GuestCheckInDocumentUploadView,
    GuestCheckInJobPollView,
    GuestCheckInOccupancyView,
    GuestCheckInProgressView,
    GuestCheckInSessionView,
    GuestCheckInSlotCommitView,
    GuestCheckInSlotView,
)
from apps.api.guest_payment_views import GuestPaymentView
from apps.api.offer_views import PublicOfferPdfView
from apps.api.guest_portal_views import (
    GuestPortalEntranceImageView,
    GuestPortalKeyGuideStepImageView,
    GuestPortalView,
)

urlpatterns = [
    path("auth/reception-login/", ReceptionLoginView.as_view(), name="reception-login"),
    path("auth/reception-logout/", ReceptionLogoutView.as_view(), name="reception-logout"),
    path("auth/reception-session/", ReceptionSessionView.as_view(), name="reception-session"),
    path("integrations/", include("apps.api.integrations_urls")),
    path("platform/", include("apps.api.platform_urls")),
    path("reception/", include("apps.api.reception_urls")),
    path("rooms/", include("apps.api.rooms_urls")),
    path("app/config", AppConfigView.as_view(), name="app-config"),
    path("app/fcm-token", FcmTokenRegisterView.as_view(), name="app-fcm-token"),
    path(
        "public/site-context/",
        SiteContextView.as_view(),
        name="public-site-context",
    ),
    path("public/properties", PublicPropertiesView.as_view(), name="public-properties"),
    path("public/units", PublicUnitsView.as_view(), name="public-units"),
    path(
        "public/availability",
        PublicAvailabilityView.as_view(),
        name="public-availability",
    ),
    path(
        "public/reservations",
        PublicReservationCreateView.as_view(),
        name="public-reservations",
    ),
    path(
        "public/invoices/<uuid:public_access_token>/",
        PublicInvoiceHtmlView.as_view(),
        name="public-invoice-html",
    ),
    path(
        "public/invoices/<uuid:public_access_token>/pdf/",
        PublicInvoicePdfView.as_view(),
        name="public-invoice-pdf",
    ),
    path(
        "public/reservations/<str:booking_code>",
        PublicReservationStatusView.as_view(),
        name="public-reservation-status",
    ),
    path(
        "public/check-in/<uuid:token>/",
        GuestCheckInSessionView.as_view(),
        name="public-guest-checkin-session",
    ),
    path(
        "public/check-in/<uuid:token>/progress/",
        GuestCheckInProgressView.as_view(),
        name="public-guest-checkin-progress",
    ),
    path(
        "public/check-in/<uuid:token>/occupancy/",
        GuestCheckInOccupancyView.as_view(),
        name="public-guest-checkin-occupancy",
    ),
    path(
        "public/check-in/<uuid:token>/slots/<int:position>/",
        GuestCheckInSlotView.as_view(),
        name="public-guest-checkin-slot",
    ),
    path(
        "public/check-in/<uuid:token>/slots/<int:position>/commit/",
        GuestCheckInSlotCommitView.as_view(),
        name="public-guest-checkin-slot-commit",
    ),
    path(
        "public/check-in/<uuid:token>/slots/<int:position>/documents/",
        GuestCheckInDocumentUploadView.as_view(),
        name="public-guest-checkin-documents",
    ),
    path(
        "public/check-in/<uuid:token>/jobs/<int:job_id>/",
        GuestCheckInJobPollView.as_view(),
        name="public-guest-checkin-job",
    ),
    path(
        "public/check-in/<uuid:token>/complete/",
        GuestCheckInCompleteView.as_view(),
        name="public-guest-checkin-complete",
    ),
    path(
        "public/offers/<uuid:public_access_token>/pdf/",
        PublicOfferPdfView.as_view(),
        name="public-offer-pdf",
    ),
    path(
        "public/payment/<uuid:token>/",
        GuestPaymentView.as_view(),
        name="public-guest-payment",
    ),
    path(
        "public/guest-portal/<uuid:token>/",
        GuestPortalView.as_view(),
        name="public-guest-portal",
    ),
    path(
        "public/guest-portal/<uuid:token>/entrance/",
        GuestPortalEntranceImageView.as_view(),
        name="public-guest-portal-entrance",
    ),
    path(
        "public/guest-portal/<uuid:token>/steps/<int:index>/",
        GuestPortalKeyGuideStepImageView.as_view(),
        name="public-guest-portal-key-guide-step",
    ),
]
