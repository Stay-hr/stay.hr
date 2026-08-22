"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { BookingPdfImportForm } from "@/app/_components/BookingPdfImportForm";
import { CountryFlag } from "@/app/_components/CountryFlag";
import { GuestCheckInProgressSection } from "@/app/_components/GuestCheckInProgressSection";
import { GuestList } from "@/app/_components/GuestList";
import { GuestMessagesPanel } from "@/app/_components/GuestMessagesPanel";
import { GuestReviewsPanel } from "@/app/_components/GuestReviewsPanel";
import { ReservationFinancialSection } from "@/app/_components/ReservationFinancialSection";
import { ReservationOfferSection } from "@/app/_components/ReservationOfferSection";
import { ReservationPaymentSendSection } from "@/app/_components/ReservationPaymentSendSection";
import { ReservationInvoiceSection } from "@/app/_components/ReservationInvoiceSection";
import { ReservationMoveDatesModal } from "@/app/_components/ReservationMoveDatesModal";
import { useImportSourceLabel, useReservationStatusLabel } from "@/lib/i18n-ui";
import { useReservationVersionWatch } from "@/lib/useReservationVersionWatch";
import {
  checkInBlockedMessageKey,
  isCheckInActionDisabled,
  showCheckInBlockedHint,
} from "@/lib/checkInEligibility";
import { LinkifiedText } from "@/lib/linkifyText";
import { reservationConfirmationPdfPath } from "@/lib/stay-client";
import {
  allowedNextStatuses,
  statusActionKey,
  statusConfirmKey,
  statusSuccessKey,
} from "@/lib/reservationStatusTransitions";
import type {
  AppConfig,
  BookingPdfImportResult,
  EvisitorCheckinResult,
  ReservationDetail,
  ReservationStatus,
} from "@/lib/types";
import { reservationStatusClass } from "@/lib/reservationUi";

function checkInSuccessMessage(
  t: (key: string, values?: Record<string, string | number>) => string,
  evisitorCheckin: EvisitorCheckinResult | null | undefined,
): string {
  if (!evisitorCheckin) {
    return t("checkInSuccess");
  }
  const required =
    evisitorCheckin.submitted + evisitorCheckin.failed + evisitorCheckin.validation_failed;
  switch (evisitorCheckin.overall) {
    case "complete":
      return t("checkInSuccessEvisitorComplete");
    case "partial":
      return t("checkInSuccessEvisitorPartial", {
        submitted: evisitorCheckin.submitted,
        required,
      });
    case "none":
      return t("checkInSuccessEvisitorNone");
    case "not_required":
      return t("checkInSuccessEvisitorNotRequired");
    default:
      return t("checkInSuccess");
  }
}

type Props = {
  reservationId: number;
  embedded?: boolean;
  onUpdated?: () => void | Promise<void>;
};

export function ReservationDetailPanel({ reservationId, embedded = false, onUpdated }: Props) {
  const router = useRouter();
  const t = useTranslations("reservation");
  const tc = useTranslations("common");
  const statusLabel = useReservationStatusLabel();
  const importSourceLabel = useImportSourceLabel();
  const [reservation, setReservation] = useState<ReservationDetail | null>(null);
  const [error, setError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [statusChanging, setStatusChanging] = useState(false);
  const [moveDatesOpen, setMoveDatesOpen] = useState(false);
  const [checkoutFailedGuestIds, setCheckoutFailedGuestIds] = useState<number[]>([]);
  const [guestInvoices, setGuestInvoices] = useState(false);
  const [bookingOffers, setBookingOffers] = useState(false);
  const [channelManager, setChannelManager] = useState<string | undefined>();

  const load = useCallback(async (options?: { background?: boolean }) => {
    const background = options?.background ?? false;
    if (!background) {
      setLoading(true);
    }
    setError("");
    try {
      const res = await fetch(`/api/stay/reception/reservations/${reservationId}/`);
      if (!res.ok) throw new Error(t("notFound"));
      setReservation((await res.json()) as ReservationDetail);
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
      if (!background) {
        setReservation(null);
      }
    } finally {
      if (!background) {
        setLoading(false);
      }
    }
  }, [reservationId, t, tc]);

  useReservationVersionWatch({
    reservationId,
    scope: "payments",
    transport: "poll",
    onVersionChange: () => {
      void load({ background: true });
    },
  });

  useEffect(() => {
    void fetch("/api/stay/app/config")
      .then((res) => (res.ok ? res.json() : null))
      .then((config: AppConfig | null) => {
        setGuestInvoices(Boolean(config?.feature_flags?.guest_invoices));
        setBookingOffers(Boolean(config?.feature_flags?.booking_offers));
        setChannelManager(config?.channel_manager);
      })
      .catch(() => {
        setGuestInvoices(false);
        setChannelManager(undefined);
      });
  }, []);

  useEffect(() => {
    setMoveDatesOpen(false);
    setActionMessage("");
    void load();
  }, [load]);

  const primaryUnitId = reservation?.units?.find((unit) => unit.room)?.room ?? null;
  const canMoveDates = reservation?.status === "expected" && primaryUnitId !== null;
  const nextStatuses = reservation ? allowedNextStatuses(reservation.status) : [];
  const showCheckoutHint =
    reservation?.status === "checked_in" &&
    reservation.evisitor_summary != null &&
    reservation.evisitor_summary !== "complete" &&
    reservation.evisitor_summary !== "checked_out";
  const showEvisitorProgress =
    reservation?.evisitor_progress != null &&
    reservation.evisitor_progress.required > 0 &&
    reservation.evisitor_summary === "incomplete";
  const showCheckInHint = reservation ? showCheckInBlockedHint(reservation) : false;
  const checkInHintKey = reservation
    ? checkInBlockedMessageKey(reservation.check_in_blocked_code)
    : null;

  async function handleMoveDatesSuccess(updated: ReservationDetail) {
    setReservation(updated);
    setMoveDatesOpen(false);
    setActionMessage(t("moveDatesSuccess"));
    await onUpdated?.();
  }

  function handleImportSuccess(result: BookingPdfImportResult) {
    if (result.id !== reservationId && !embedded) {
      router.push(`/reservations/${result.id}`);
      return;
    }
    setReservation(result);
    setActionMessage(t("importPdfSuccess"));
    void onUpdated?.();
  }

  async function patchStatus(newStatus: ReservationStatus) {
    if (!reservation) return;

    let waivedFees: boolean | undefined;
    if (newStatus === "no_show") {
      const confirmKey = statusConfirmKey(newStatus);
      if (confirmKey && !window.confirm(t(confirmKey))) return;

      if (reservation.import_source === "channex") {
        if (window.confirm(t("noShowConfirmWaive"))) {
          waivedFees = true;
        } else if (window.confirm(t("noShowConfirmCharge"))) {
          waivedFees = false;
        } else {
          return;
        }
      }
    } else {
      const confirmKey = statusConfirmKey(newStatus);
      if (confirmKey && !window.confirm(t(confirmKey))) return;
    }

    setStatusChanging(true);
    setActionMessage("");
    setError("");
    setCheckoutFailedGuestIds([]);
    try {
      const payload: { status: ReservationStatus; waived_fees?: boolean } = {
        status: newStatus,
      };
      if (newStatus === "no_show" && waivedFees !== undefined) {
        payload.waived_fees = waivedFees;
      }

      const res = await fetch(`/api/stay/reception/reservations/${reservation.id}/`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as {
          status?:
            | string
            | string[]
            | {
                code?: string;
                message?: string;
                failed_guests?: Array<{ guest_id?: number; name?: string; reason?: string }>;
              };
          detail?: string;
        } | null;
        const statusError = data?.status;
        if (
          statusError &&
          typeof statusError === "object" &&
          !Array.isArray(statusError) &&
          statusError.code === "evisitor_checkout_failed"
        ) {
          const failed = statusError.failed_guests || [];
          setCheckoutFailedGuestIds(
            failed
              .map((g) => g.guest_id)
              .filter((id): id is number => typeof id === "number"),
          );
          const lines = [
            statusError.message || t("statusChangeFailed"),
            ...failed.map((g) => `${g.name || g.guest_id}: ${g.reason || ""}`.trim()),
          ].filter(Boolean);
          throw new Error(lines.join("\n"));
        }
        const statusMessage = Array.isArray(statusError)
          ? statusError[0]
          : typeof statusError === "string"
            ? statusError
            : undefined;
        throw new Error(statusMessage || data?.detail || t("statusChangeFailed"));
      }
      const updated = (await res.json()) as ReservationDetail;
      setReservation(updated);
      if (newStatus === "checked_in") {
        setActionMessage(checkInSuccessMessage(t, updated.evisitor_checkin));
      } else {
        const successKey = statusSuccessKey(newStatus);
        setActionMessage(successKey ? t(successKey) : t("statusChangeFailed"));
      }
      await onUpdated?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("statusChangeFailed"));
      await load();
    } finally {
      setStatusChanging(false);
    }
  }

  if (loading) {
    return <p className="text-sm text-muted">{tc("loading")}</p>;
  }

  if (error && !reservation) {
    return <p className="text-sm text-red-600">{error}</p>;
  }

  if (!reservation) return null;

  return (
    <div className="space-y-4">
      {actionMessage ? <p className="text-sm text-emerald-700">{actionMessage}</p> : null}
      {error ? <p className="whitespace-pre-wrap text-sm text-red-600">{error}</p> : null}

      {!embedded ? (
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold text-stay-navy">
            <CountryFlag iso2={reservation.primary_guest_nationality_iso2} size="md" />
            <span>{reservation.primary_guest_name || reservation.room_name}</span>
          </h1>
          <p className="text-muted">
            #{reservation.id} · {reservation.external_id || tc("dash")}
          </p>
        </div>
      ) : (
        <p className="text-sm text-muted">
          #{reservation.id} · {reservation.external_id || tc("dash")}
        </p>
      )}

      <dl className="grid gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted">{t("room")}</dt>
          <dd className="font-medium">{reservation.room_name}</dd>
        </div>
        <div>
          <dt className="text-muted">{t("status")}</dt>
          <dd>
            <span className={`badge ${reservationStatusClass(reservation.status)}`}>
              {statusLabel(reservation.status)}
            </span>
            {showEvisitorProgress && reservation.evisitor_progress ? (
              <span className="badge badge-expected ml-2 text-xs">
                {t("evisitorProgress", {
                  sent: reservation.evisitor_progress.sent,
                  required: reservation.evisitor_progress.required,
                })}
              </span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt className="text-muted">{t("importSource")}</dt>
          <dd className="font-medium">
            {importSourceLabel(reservation.import_source, reservation.source)}
          </dd>
        </div>
        <div>
          <dt className="text-muted">{t("checkIn")}</dt>
          <dd className="font-medium">{reservation.check_in_date}</dd>
        </div>
        <div>
          <dt className="text-muted">{t("checkOut")}</dt>
          <dd className="font-medium">{reservation.check_out_date}</dd>
        </div>
        <div>
          <dt className="text-muted">{t("booker")}</dt>
          <dd className="font-medium">{reservation.booker_name || tc("dash")}</dd>
        </div>
        <div>
          <dt className="text-muted">{t("phone")}</dt>
          <dd className="font-medium">{reservation.booker_phone || tc("dash")}</dd>
        </div>
      </dl>

      {reservation.checkin_progress ? (
        <GuestCheckInProgressSection
          reservationId={reservation.id}
          reservationStatus={reservation.status}
          progress={reservation.checkin_progress}
          onUpdated={() => load({ background: true })}
        />
      ) : null}

      <ReservationFinancialSection reservation={reservation} />

      <ReservationPaymentSendSection
        reservation={reservation}
        onSent={() => load({ background: true })}
      />

      {bookingOffers ? (
        <ReservationOfferSection
          reservation={reservation}
          onUpdated={() => load({ background: true })}
        />
      ) : null}

      <GuestMessagesPanel reservationId={reservation.id} />

      {channelManager === "channex" ? (
        <GuestReviewsPanel reservationId={reservation.id} />
      ) : null}

      {reservation.pdf_imported_at || reservation.confirmation_pdf_url ? (
        <p>
          <a
            href={reservationConfirmationPdfPath(reservation.id)}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm font-medium text-stay-blue hover:underline"
          >
            {t("downloadPdf")}
          </a>
        </p>
      ) : null}

      {guestInvoices && reservation.status === "checked_out" ? (
        <ReservationInvoiceSection reservation={reservation} onReservationUpdated={load} />
      ) : null}

      {canMoveDates || nextStatuses.length > 0 ? (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {canMoveDates ? (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setMoveDatesOpen(true);
                  setActionMessage("");
                }}
              >
                {t("moveDates")}
              </button>
            ) : null}
            {nextStatuses.map((nextStatus) => {
              const actionKey = statusActionKey(nextStatus);
              if (!actionKey) return null;
              const isCancel = nextStatus === "canceled";
              const isNoShow = nextStatus === "no_show";
              const isCheckIn = nextStatus === "checked_in";
              const disabled =
                statusChanging || (isCheckIn && isCheckInActionDisabled(reservation));
              return (
                <button
                  key={nextStatus}
                  type="button"
                  className={isCancel || isNoShow ? "btn-danger" : "btn btn-sm"}
                  disabled={disabled}
                  onClick={() => void patchStatus(nextStatus)}
                >
                  {t(actionKey)}
                </button>
              );
            })}
          </div>
          {showCheckInHint && checkInHintKey ? (
            <p className="text-sm text-amber-800">{t(checkInHintKey)}</p>
          ) : null}
          {showCheckoutHint ? (
            <p className="text-sm text-amber-800">{t("checkoutEvisitorHint")}</p>
          ) : null}
        </div>
      ) : null}

      <div>
        <h2 className="mb-2 font-semibold">
          {t("guestsTitle", { count: reservation.guests?.length || 0 })}
        </h2>
        <GuestList
          reservationId={reservation.id}
          guests={reservation.guests || []}
          reservationStatus={reservation.status}
          onGuestUpdated={load}
          highlightGuestIds={checkoutFailedGuestIds}
        />
      </div>

      {reservation.notes ? (
        <div>
          <h2 className="mb-1 font-semibold">{t("notes")}</h2>
          <LinkifiedText className="whitespace-pre-wrap text-sm text-muted">
            {reservation.notes}
          </LinkifiedText>
        </div>
      ) : null}

      <BookingPdfImportForm
        propertySlug={reservation.property_slug}
        reservationId={reservation.id}
        expectedBookingNumber={reservation.external_id || reservation.booking_code}
        onSuccess={handleImportSuccess}
        compact
      />

      <p className="text-xs text-stay-muted/70">{t("actionsHint")}</p>

      {canMoveDates && primaryUnitId ? (
        <ReservationMoveDatesModal
          open={moveDatesOpen}
          onClose={() => setMoveDatesOpen(false)}
          reservationId={reservation.id}
          unitId={primaryUnitId}
          checkIn={reservation.check_in_date}
          checkOut={reservation.check_out_date}
          onSuccess={(updated) => void handleMoveDatesSuccess(updated)}
        />
      ) : null}
    </div>
  );
}
