"use client";

import { useTranslations } from "next-intl";
import { timelineEvisitorFailedGuests } from "@/lib/evisitorTimelineBadge";
import type { Reservation } from "@/lib/types";

type Props = {
  open: boolean;
  reservation: Reservation | null;
  onClose: () => void;
};

export function EvisitorTimelineErrorModal({ open, reservation, onClose }: Props) {
  const t = useTranslations("guest");
  const tc = useTranslations("common");

  if (!open || !reservation) return null;

  const failedGuests = timelineEvisitorFailedGuests(reservation.guests);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div
        className="card flex w-full max-w-md flex-col overflow-visible"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evisitor-timeline-error-title"
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 id="evisitor-timeline-error-title" className="font-semibold text-stay-navy">
            {t("evisitorError")}
          </h2>
          <button
            type="button"
            className="btn-ghost px-2"
            onClick={onClose}
            aria-label={tc("close")}
          >
            ×
          </button>
        </div>

        <div className="space-y-3 px-4 py-4">
          {failedGuests.length === 0 ? (
            <p className="text-sm text-muted">{t("evisitorFailed")}</p>
          ) : (
            <ul className="space-y-3">
              {failedGuests.map((guest) => {
                const name = `${guest.first_name} ${guest.last_name}`.trim() || tc("dash");
                const errorText = guest.evisitor_error?.trim() || t("evisitorFailed");
                return (
                  <li key={guest.id} className="text-sm">
                    <p className="font-medium text-stay-navy">{name}</p>
                    <p className="mt-0.5 whitespace-pre-wrap text-red-700">{errorText}</p>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="flex justify-end pt-1">
            <button type="button" className="btn btn-sm" onClick={onClose}>
              {tc("close")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
