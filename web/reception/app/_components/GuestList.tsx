"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { CountryFlag } from "@/app/_components/CountryFlag";
import { GuestAvatar } from "@/app/_components/GuestAvatar";
import type { GuestLite, ReservationStatus } from "@/lib/types";

type DetailRow = {
  label: string;
  value: string | null | undefined;
  fullWidth?: boolean;
  isError?: boolean;
  preserveWhitespace?: boolean;
};

type EvisitorSubmitResponse = {
  status?: string;
  message?: string;
  user_message?: string;
  recovered?: boolean;
  field_errors?: Record<string, string>;
};

type GuestMessage = {
  type: "success" | "error";
  text: string;
  fieldErrors?: Record<string, string>;
};

type Props = {
  reservationId: number;
  guests: GuestLite[];
  reservationStatus?: ReservationStatus;
  onGuestUpdated?: () => void | Promise<void>;
  highlightGuestIds?: number[];
};

const EVISITOR_SUBMITTABLE_STATUSES = new Set<ReservationStatus>(["expected", "checked_in"]);
const EVISITOR_DONE_STATUSES = new Set(["sent", "checked_out", "checkout_failed"]);

const EVISITOR_FIELD_LABEL_KEYS: Record<string, string> = {
  first_name: "evisitorFieldFirstName",
  last_name: "evisitorFieldLastName",
  sex: "evisitorFieldSex",
  date_of_birth: "evisitorFieldDateOfBirth",
  nationality: "evisitorFieldNationality",
  document_type: "evisitorFieldDocumentType",
  document_number: "evisitorFieldDocumentNumber",
  facility: "evisitorFieldFacility",
  stay_dates: "evisitorFieldStayDates",
  address: "evisitorFieldAddress",
  date_of_expiry: "evisitorFieldDateOfExpiry",
};

export function GuestList({
  reservationId,
  guests,
  reservationStatus,
  onGuestUpdated,
  highlightGuestIds,
}: Props) {
  const t = useTranslations("guest");
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [submittingGuestId, setSubmittingGuestId] = useState<number | null>(null);
  const [guestMessages, setGuestMessages] = useState<Record<number, GuestMessage>>({});

  function evisitorStatusLabel(status: string): string {
    const map: Record<string, string> = {
      not_sent: t("evisitorNotSent"),
      pending: t("evisitorPending"),
      sent: t("evisitorSent"),
      checked_out: t("evisitorCheckedOut"),
      failed: t("evisitorFailed"),
      checkout_failed: t("evisitorCheckoutFailed"),
      submitted: t("evisitorSent"),
      complete: t("evisitorSent"),
    };
    return map[status] || status || "—";
  }

  function guestRequiresEvisitor(guest: GuestLite): boolean {
    return guest.evisitor_required !== false;
  }

  function evisitorDisplayStatus(guest: GuestLite, status: string): string {
    if (!guestRequiresEvisitor(guest)) {
      return t("evisitorNotRequired");
    }
    if (status === "not_sent") {
      return t("evisitorUnregistered");
    }
    return evisitorStatusLabel(status);
  }

  function canShowEvisitorSubmit(guest: GuestLite, status: string): boolean {
    if (!guestRequiresEvisitor(guest)) {
      return false;
    }
    if (!reservationStatus || !EVISITOR_SUBMITTABLE_STATUSES.has(reservationStatus)) {
      return false;
    }
    return !EVISITOR_DONE_STATUSES.has(status);
  }

  function canShowEvisitorCheckoutRetry(guest: GuestLite, status: string): boolean {
    if (!guestRequiresEvisitor(guest)) {
      return false;
    }
    if (reservationStatus !== "checked_in") {
      return false;
    }
    return status === "checkout_failed";
  }

  function showEvisitorBadge(guest: GuestLite, status: string): boolean {
    if (!guestRequiresEvisitor(guest)) {
      return true;
    }
    return (
      status === "sent" ||
      status === "failed" ||
      status === "checkout_failed" ||
      status === "checked_out" ||
      status === "not_sent" ||
      status === "pending"
    );
  }

  function evisitorStatusBadgeClass(guest: GuestLite, status: string): string {
    if (!guestRequiresEvisitor(guest)) {
      return "badge text-xs bg-slate-100 text-slate-600";
    }
    if (status === "sent" || status === "checked_out") {
      return "badge badge-checked_in text-xs";
    }
    if (status === "checkout_failed") {
      return "badge text-xs bg-amber-50 text-amber-800 ring-1 ring-amber-200";
    }
    if (status === "failed") {
      return "badge badge-canceled text-xs";
    }
    if (status === "pending") {
      return "badge badge-expected text-xs";
    }
    if (status === "not_sent") {
      return "badge text-xs bg-amber-50 text-amber-800 ring-1 ring-amber-200";
    }
    return "";
  }

  function evisitorFieldLabel(field: string): string {
    const key = EVISITOR_FIELD_LABEL_KEYS[field];
    return key ? t(key) : field;
  }

  function formatEvisitorSubmitError(reason: string | undefined): string {
    const detail = reason?.trim();
    if (!detail || detail === "failed") {
      return t("evisitorSubmitFailed");
    }
    return t("evisitorSubmitFailedWithReason", { reason: detail });
  }

  function sexLabel(sex: string): string {
    const value = sex.trim().toUpperCase();
    if (value === "M") return t("sexMale");
    if (value === "F") return t("sexFemale");
    return sex;
  }

  function guestDetailRows(guest: GuestLite): DetailRow[] {
    const rows: DetailRow[] = [
      { label: t("email"), value: guest.email },
      { label: t("phone"), value: guest.phone },
      { label: t("dateOfBirth"), value: guest.date_of_birth },
      { label: t("sex"), value: guest.sex ? sexLabel(guest.sex) : "" },
      { label: t("documentType"), value: guest.document_type },
      { label: t("documentNumber"), value: guest.document_number },
      { label: t("dateOfIssue"), value: guest.date_of_issue },
      { label: t("dateOfExpiry"), value: guest.date_of_expiry },
      { label: t("issuingAuthority"), value: guest.issuing_authority },
      { label: t("personalId"), value: guest.personal_id_number },
      { label: t("address"), value: guest.address, fullWidth: true },
      { label: t("evisitor"), value: evisitorDisplayStatus(guest, guest.evisitor_status) },
    ];

    if (guestRequiresEvisitor(guest) && guest.evisitor_error) {
      rows.push({
        label: t("evisitorError"),
        value: guest.evisitor_error,
        fullWidth: true,
        isError: true,
        preserveWhitespace: true,
      });
    }

    return rows.filter((row) => row.value?.trim());
  }

  function toggleGuest(id: number) {
    setExpandedId((current) => (current === id ? null : id));
  }

  useEffect(() => {
    if (highlightGuestIds && highlightGuestIds.length > 0) {
      setExpandedId(highlightGuestIds[0]);
    }
  }, [highlightGuestIds]);

  async function handleEvisitorSubmit(guest: GuestLite) {
    const forceRetry = guest.evisitor_status === "failed";
    setSubmittingGuestId(guest.id);
    setGuestMessages((current) => {
      const next = { ...current };
      delete next[guest.id];
      return next;
    });

    try {
      const res = await fetch(
        `/api/stay/reception/reservations/${reservationId}/guests/${guest.id}/evisitor-submit/`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(forceRetry ? { force_retry: true } : {}),
        },
      );

      const data = (await res.json().catch(() => null)) as EvisitorSubmitResponse | null;

      if (res.ok && data?.status === "sent") {
        setGuestMessages({
          [guest.id]: {
            type: "success",
            text: data.recovered
              ? data.message || t("evisitorRecovered")
              : data.message || t("evisitorSubmitSuccess"),
          },
        });
        await onGuestUpdated?.();
        return;
      }

      if (data?.status === "validation_failed" && data.field_errors) {
        setGuestMessages({
          [guest.id]: {
            type: "error",
            text: data.message?.trim() || t("evisitorValidationFailed"),
            fieldErrors: data.field_errors,
          },
        });
        return;
      }

      const reason = data?.user_message || data?.message || "";
      setGuestMessages({
        [guest.id]: { type: "error", text: formatEvisitorSubmitError(reason) },
      });
    } catch {
      setGuestMessages({
        [guest.id]: { type: "error", text: t("evisitorSubmitFailed") },
      });
    } finally {
      setSubmittingGuestId(null);
    }
  }

  async function handleEvisitorCheckout(guest: GuestLite) {
    setSubmittingGuestId(guest.id);
    setGuestMessages((current) => {
      const next = { ...current };
      delete next[guest.id];
      return next;
    });

    try {
      const res = await fetch(
        `/api/stay/reception/reservations/${reservationId}/guests/${guest.id}/evisitor-checkout/`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        },
      );
      const data = (await res.json().catch(() => null)) as EvisitorSubmitResponse | null;

      if (res.ok && data?.status === "checked_out") {
        setGuestMessages({
          [guest.id]: {
            type: "success",
            text: data.message || t("evisitorCheckoutSuccess"),
          },
        });
        await onGuestUpdated?.();
        return;
      }

      const reason = data?.user_message || data?.message || "";
      setGuestMessages({
        [guest.id]: {
          type: "error",
          text: reason
            ? t("evisitorCheckoutFailedWithReason", { reason })
            : t("evisitorCheckoutRetryFailed"),
        },
      });
      await onGuestUpdated?.();
    } catch {
      setGuestMessages({
        [guest.id]: { type: "error", text: t("evisitorCheckoutRetryFailed") },
      });
    } finally {
      setSubmittingGuestId(null);
    }
  }

  return (
    <ul className="divide-y divide-stay-border rounded-xl border border-stay-border">
      {guests.map((guest) => {
        const expanded = expandedId === guest.id;
        const panelId = `guest-details-${guest.id}`;
        const fullName = `${guest.first_name} ${guest.last_name}`.trim();
        const detailRows = guestDetailRows(guest);
        const evisitorStatus = guest.evisitor_status || "not_sent";
        const showSubmit = canShowEvisitorSubmit(guest, evisitorStatus);
        const showCheckoutRetry = canShowEvisitorCheckoutRetry(guest, evisitorStatus);
        const showBadge = showEvisitorBadge(guest, evisitorStatus);
        const isSubmitting = submittingGuestId === guest.id;
        const guestMessage = guestMessages[guest.id];
        const isHighlighted = Boolean(highlightGuestIds?.includes(guest.id));

        return (
          <li
            key={guest.id}
            className={isHighlighted ? "bg-amber-50/60 ring-1 ring-inset ring-amber-200" : undefined}
          >
            <button
              type="button"
              className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm transition hover:bg-slate-50"
              onClick={() => toggleGuest(guest.id)}
              aria-expanded={expanded}
              aria-controls={panelId}
            >
              <span className="flex min-w-0 items-center gap-2">
                <GuestAvatar
                  reservationId={reservationId}
                  guestId={guest.id}
                  name={fullName}
                  facePhotoUrl={guest.face_photo_url}
                  size="sm"
                />
                <CountryFlag iso2={guest.nationality} />
                <span className="truncate">
                  {fullName}
                  {guest.is_primary ? <span className="text-muted"> · {t("primary")}</span> : null}
                </span>
                {showBadge ? (
                  <span className={evisitorStatusBadgeClass(guest, evisitorStatus)}>
                    {evisitorDisplayStatus(guest, evisitorStatus)}
                  </span>
                ) : null}
              </span>
              <span className="shrink-0 text-muted" aria-hidden="true">
                {expanded ? "▾" : "▸"}
              </span>
            </button>

            {expanded ? (
              <div id={panelId} className="border-t border-stay-border bg-slate-50/80 px-3 py-3">
                <div className="mb-3 flex items-center gap-3">
                  <GuestAvatar
                    reservationId={reservationId}
                    guestId={guest.id}
                    name={fullName}
                    facePhotoUrl={guest.face_photo_url}
                    size="lg"
                  />
                  <div className="min-w-0">
                    <p className="font-semibold text-stay-navy">{fullName}</p>
                    <p className="flex items-center gap-1.5 text-sm text-muted">
                      <CountryFlag iso2={guest.nationality} />
                      {guest.is_primary ? ` · ${t("primary")}` : null}
                    </p>
                  </div>
                </div>

                {detailRows.length > 0 ? (
                  <dl className="grid gap-2 text-sm sm:grid-cols-2">
                    {detailRows.map((row) => (
                      <div key={row.label} className={row.fullWidth ? "sm:col-span-2" : undefined}>
                        <dt className="text-muted">{row.label}</dt>
                        <dd
                          className={`font-medium ${row.isError ? "text-red-700" : ""} ${
                            row.preserveWhitespace ? "whitespace-pre-wrap" : ""
                          }`}
                        >
                          {row.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="text-sm text-muted">{t("noExtraData")}</p>
                )}

                {guestMessage ? (
                  <div
                    className={`mt-3 text-sm ${
                      guestMessage.type === "success" ? "text-emerald-700" : "text-red-600"
                    }`}
                  >
                    <p>{guestMessage.text}</p>
                    {guestMessage.fieldErrors ? (
                      <ul className="mt-2 list-disc space-y-1 pl-5">
                        {Object.entries(guestMessage.fieldErrors).map(([field, error]) => (
                          <li key={field}>
                            <span className="font-medium">{evisitorFieldLabel(field)}:</span> {error}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}

                {showSubmit ? (
                  <div className="mt-3">
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={isSubmitting}
                      onClick={(event) => {
                        event.stopPropagation();
                        void handleEvisitorSubmit(guest);
                      }}
                    >
                      {isSubmitting
                        ? t("evisitorSubmitting")
                        : evisitorStatus === "failed"
                          ? t("evisitorRetry")
                          : t("evisitorSubmit")}
                    </button>
                  </div>
                ) : null}

                {showCheckoutRetry ? (
                  <div className="mt-3">
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={isSubmitting}
                      onClick={(event) => {
                        event.stopPropagation();
                        void handleEvisitorCheckout(guest);
                      }}
                    >
                      {isSubmitting ? t("evisitorSubmitting") : t("evisitorRetryCheckout")}
                    </button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
