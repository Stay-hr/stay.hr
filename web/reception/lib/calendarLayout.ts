import { addDaysIso, addMonthsIso } from "@/lib/utils";

export type BarSpan = {
  startCol: number;
  spanDays: number;
};

export const ROLLING_WINDOW_DAYS = 30;
export const HISTORY_LOOKBACK_DAYS = 365;

export type CalendarDay = {
  iso: string;
  dayOfMonth: number;
  weekday: number;
  isWeekend: boolean;
  monthKey: string;
  showMonthLabel: boolean;
};

function compareIso(a: string, b: string): number {
  return a.localeCompare(b);
}

export function maxDate(a: string, b: string): string {
  return compareIso(a, b) >= 0 ? a : b;
}

export function operationalFloorIso(today: string): string {
  return addDaysIso(today, -1);
}

export function historyMinStartIso(floor: string): string {
  return addDaysIso(floor, -HISTORY_LOOKBACK_DAYS);
}

export function clampRangeStart(next: string, minStart: string): string {
  return maxDate(minStart, next);
}

export function canGoPrev(rangeStart: string, minStart: string): boolean {
  return rangeStart > minStart;
}

export function shouldShowHistoryPrefix(rangeStart: string, floor: string): boolean {
  return rangeStart < floor;
}

export function minDate(a: string, b: string): string {
  return compareIso(a, b) <= 0 ? a : b;
}

export function daysBetween(fromIso: string, toIso: string): number {
  const from = new Date(`${fromIso}T12:00:00Z`);
  const to = new Date(`${toIso}T12:00:00Z`);
  return Math.round((to.getTime() - from.getTime()) / 86_400_000);
}

export function monthEndExclusive(monthStart: string): string {
  return addMonthsIso(monthStart, 1);
}

function calendarDayFromIso(iso: string, showMonthLabel: boolean): CalendarDay {
  const d = new Date(`${iso}T12:00:00Z`);
  const weekday = d.getUTCDay();
  return {
    iso,
    dayOfMonth: d.getUTCDate(),
    weekday,
    isWeekend: weekday === 0 || weekday === 6,
    monthKey: iso.slice(0, 7),
    showMonthLabel,
  };
}

export function daysInMonth(monthStart: string): CalendarDay[] {
  const end = monthEndExclusive(monthStart);
  const days: CalendarDay[] = [];
  let cursor = monthStart;
  while (cursor < end) {
    const monthKey = cursor.slice(0, 7);
    const prevMonthKey = days.length > 0 ? days[days.length - 1].monthKey : null;
    const showMonthLabel =
      days.length === 0 ? cursor.endsWith("-01") : monthKey !== prevMonthKey;
    days.push(calendarDayFromIso(cursor, showMonthLabel));
    cursor = addDaysIso(cursor, 1);
  }
  return days;
}

export function daysInRange(fromIso: string, count: number): CalendarDay[] {
  const days: CalendarDay[] = [];
  for (let i = 0; i < count; i++) {
    const cursor = addDaysIso(fromIso, i);
    const monthKey = cursor.slice(0, 7);
    const prevMonthKey = i > 0 ? days[i - 1].monthKey : null;
    const showMonthLabel =
      (i > 0 && monthKey !== prevMonthKey) ||
      (i === 0 && cursor.endsWith("-01"));
    days.push(calendarDayFromIso(cursor, showMonthLabel));
  }
  return days;
}

export function barSpan(
  checkIn: string,
  checkOut: string,
  monthStart: string,
  monthEnd: string,
): BarSpan | null {
  if (checkOut <= monthStart || checkIn >= monthEnd) return null;
  const start = maxDate(checkIn, monthStart);
  const end = minDate(checkOut, monthEnd);
  const startCol = daysBetween(monthStart, start);
  const spanDays = daysBetween(start, end);
  if (spanDays <= 0) return null;
  return { startCol, spanDays };
}

export function overlapsDay(checkIn: string, checkOut: string, dayIso: string): boolean {
  const nextDay = addDaysIso(dayIso, 1);
  return checkIn < nextDay && checkOut > dayIso;
}

export function weekdayLabelHr(weekday: number): string {
  const labels = ["Ned", "Pon", "Uto", "Sri", "Čet", "Pet", "Sub"];
  return labels[weekday] || "";
}
