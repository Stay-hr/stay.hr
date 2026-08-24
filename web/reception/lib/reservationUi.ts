import type { ReservationChannelKey, ReservationStatus } from "@/lib/types";

export const statusClass: Record<string, string> = {
  expected: "badge-expected",
  checked_in: "badge-checked_in",
  checked_out: "badge-checked_out",
  canceled: "badge-canceled",
  no_show: "badge-no_show",
  pending: "badge-expected",
  refused: "badge-canceled",
};

export const statusBarClass: Record<string, string> = {
  expected: "bg-amber-200 border-amber-400 text-amber-950",
  checked_in: "bg-emerald-200 border-emerald-500 text-emerald-950",
  checked_out: "bg-slate-200 border-slate-400 text-slate-800",
  canceled: "bg-red-100 border-red-300 text-red-900",
  no_show: "bg-orange-100 border-orange-300 text-orange-950",
  pending: "bg-amber-200 border-amber-400 text-amber-950",
  refused: "bg-red-100 border-red-300 text-red-900",
};

export function reservationStatusClass(status: string): string {
  return statusClass[status] || "badge-expected";
}

export function reservationStatusBarClass(status: string): string {
  return statusBarClass[status] || statusBarClass.expected;
}

export type ImportSourceKey =
  | "booking_pdf"
  | "booking_xls"
  | "channex"
  | "web"
  | "manual";

export function importSourceKey(
  importSource: string | null | undefined,
  source: string | null | undefined,
): ImportSourceKey {
  const normalized = (importSource || "").trim().toLowerCase();
  if (normalized === "booking_pdf") return "booking_pdf";
  if (normalized === "booking_xls") return "booking_xls";
  if (normalized === "channex") return "channex";
  if ((source || "").trim().toLowerCase() === "api") return "web";
  return "manual";
}

export function channelBadgeClass(key: ReservationChannelKey | string): string {
  if (key === "booking_com") {
    return "badge bg-sky-50 text-sky-800 ring-1 ring-sky-200";
  }
  if (key === "airbnb") {
    return "badge bg-rose-50 text-rose-800 ring-1 ring-rose-200";
  }
  if (key === "expedia") {
    return "badge bg-yellow-50 text-yellow-900 ring-1 ring-yellow-200";
  }
  if (key === "web") {
    return "badge bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200";
  }
  if (key === "reception") {
    return "badge bg-slate-100 text-slate-800 ring-1 ring-slate-200";
  }
  return "badge bg-violet-50 text-violet-800 ring-1 ring-violet-200";
}

export type { ReservationStatus };
