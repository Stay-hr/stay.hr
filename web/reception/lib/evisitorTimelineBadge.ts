import type { EvisitorProgress, EvisitorSummary, GuestLite, ReservationStatus } from "@/lib/types";

export type TimelineEvisitorTone = "ok" | "pending" | "error";

export type TimelineEvisitorBadgeInput = {
  status: ReservationStatus | string;
  evisitor_summary?: EvisitorSummary;
  evisitor_progress?: EvisitorProgress;
};

/**
 * Timeline eVisitor badge tone for checked-in reservations.
 * `failed > 0` always wins over pending when summary is incomplete.
 * No badge when progress is missing or required === 0 (avoids eVisitor 0/0).
 */
export function timelineEvisitorTone(
  reservation: TimelineEvisitorBadgeInput,
): TimelineEvisitorTone | null {
  if (reservation.status !== "checked_in") {
    return null;
  }

  const summary = reservation.evisitor_summary;
  const progress = reservation.evisitor_progress;
  if (!summary || summary === "none") {
    return null;
  }
  if (!progress || progress.required <= 0) {
    return null;
  }

  if (summary === "complete" || summary === "checked_out") {
    return "ok";
  }

  if (summary === "incomplete") {
    return progress.failed > 0 ? "error" : "pending";
  }

  return null;
}

export function timelineEvisitorLabel(progress: EvisitorProgress): string {
  return `eVisitor ${progress.sent}/${progress.required}`;
}

export function timelineEvisitorFailedGuests(guests: GuestLite[] | undefined): GuestLite[] {
  return (guests ?? []).filter(
    (guest) =>
      guest.evisitor_required !== false &&
      (guest.evisitor_status === "failed" || guest.evisitor_status === "checkout_failed"),
  );
}

export function timelineEvisitorBadgeClass(tone: TimelineEvisitorTone): string {
  if (tone === "ok") {
    return "badge badge-checked_in";
  }
  if (tone === "error") {
    return "badge badge-canceled";
  }
  return "badge bg-amber-50 text-amber-800 ring-1 ring-amber-200";
}
