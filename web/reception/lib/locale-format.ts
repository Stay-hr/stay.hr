import { addDaysIso } from "@/lib/utils";

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function localeTag(locale: string): string {
  return locale === "hr" ? "hr-HR" : locale === "en" ? "en-GB" : `${locale}-${locale.toUpperCase()}`;
}

/** Parse YYYY-MM-DD at UTC noon so DST/local TZ cannot shift the calendar day. */
export function parseIsoDateUtcNoon(iso: string): Date | null {
  if (!ISO_DATE_RE.test(iso)) return null;
  const d = new Date(`${iso}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return null;
  if (d.toISOString().slice(0, 10) !== iso) return null;
  return d;
}

/**
 * Localized stay range: real check-in → check-out (not exclusive-end).
 * Invalid ISO never yields "Invalid Date" — falls back to raw strings or null.
 */
export function formatStayDateRange(
  locale: string,
  checkIn: string,
  checkOut: string,
): string | null {
  const from = (checkIn ?? "").trim();
  const to = (checkOut ?? "").trim();
  const fromOk = parseIsoDateUtcNoon(from) != null;
  const toOk = parseIsoDateUtcNoon(to) != null;

  if (!fromOk || !toOk) {
    if (!from && !to) return null;
    if (from && to) return `${from} → ${to}`;
    return from || to;
  }

  const fromYear = from.slice(0, 4);
  const toYear = to.slice(0, 4);
  const fromPart = shortDateLabelForLocale(locale, from, fromYear !== toYear);
  const toPart = shortDateLabelForLocale(locale, to, true);
  return `${fromPart} → ${toPart}`;
}

/**
 * Night count from calendar dates only (UTC noon). Returns null for invalid/non-positive ranges.
 */
export function stayNightsCount(checkIn: string, checkOut: string): number | null {
  const a = parseIsoDateUtcNoon((checkIn ?? "").trim());
  const b = parseIsoDateUtcNoon((checkOut ?? "").trim());
  if (!a || !b) return null;
  const nights = Math.round((b.getTime() - a.getTime()) / 86_400_000);
  if (nights <= 0) return null;
  return nights;
}

export function monthLabelForLocale(locale: string, iso: string): string {
  const d = new Date(`${iso}T12:00:00Z`);
  const raw = new Intl.DateTimeFormat(localeTag(locale), { month: "long", year: "numeric" }).format(d);
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

export function shortMonthLabelForLocale(locale: string, iso: string): string {
  const d = new Date(`${iso}T12:00:00Z`);
  return new Intl.DateTimeFormat(localeTag(locale), { month: "short" }).format(d).toUpperCase();
}

export function shortDateLabelForLocale(
  locale: string,
  iso: string,
  includeYear = false,
): string {
  const d = new Date(`${iso}T12:00:00Z`);
  const options: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" };
  if (includeYear) {
    options.year = "numeric";
  }
  return new Intl.DateTimeFormat(localeTag(locale), options).format(d);
}

export function formatDateRangeLabel(
  locale: string,
  fromIso: string,
  toExclusiveIso: string,
): string {
  const lastDayIso = addDaysIso(toExclusiveIso, -1);
  const fromYear = fromIso.slice(0, 4);
  const toYear = lastDayIso.slice(0, 4);
  const fromPart = shortDateLabelForLocale(locale, fromIso, fromYear !== toYear);
  const toPart = shortDateLabelForLocale(locale, lastDayIso, true);
  return `${fromPart} – ${toPart}`;
}

export function weekdayLabelForLocale(locale: string, weekday: number): string {
  const base = new Date(Date.UTC(2024, 0, 7 + weekday));
  return new Intl.DateTimeFormat(localeTag(locale), { weekday: "short" }).format(base);
}
