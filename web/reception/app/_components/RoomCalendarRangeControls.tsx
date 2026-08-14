"use client";

import { useTranslations } from "next-intl";
import {
  canGoPrev,
  clampRangeStart,
  ROLLING_WINDOW_DAYS,
  shouldShowHistoryPrefix,
} from "@/lib/calendarLayout";
import { addDaysIso } from "@/lib/utils";

type Props = {
  rangeStart: string;
  rangeLabel: string;
  floor: string;
  historyMin: string;
  historyEnabled: boolean;
  onRangeStartChange: (next: string) => void;
  onHistoryEnabledChange: (next: boolean) => void;
};

export function RoomCalendarRangeControls({
  rangeStart,
  rangeLabel,
  floor,
  historyMin,
  historyEnabled,
  onRangeStartChange,
  onHistoryEnabledChange,
}: Props) {
  const t = useTranslations("calendar");
  const minStart = historyEnabled ? historyMin : floor;
  const prevEnabled = canGoPrev(rangeStart, minStart);
  const atFloor = rangeStart === floor;
  const showHistoryPrefix = shouldShowHistoryPrefix(rangeStart, floor);
  const displayLabel = showHistoryPrefix
    ? `${t("historyRangePrefix")} · ${rangeLabel}`
    : rangeLabel;

  function shiftRange(deltaDays: number) {
    onRangeStartChange(clampRangeStart(addDaysIso(rangeStart, deltaDays), minStart));
  }

  function goToday() {
    onRangeStartChange(floor);
  }

  function setHistory(next: boolean) {
    onHistoryEnabledChange(next);
    if (!next && rangeStart < floor) {
      onRangeStartChange(floor);
    }
  }

  return (
    <>
      <div className="flex items-center gap-1">
        <button
          type="button"
          className="btn-ghost px-2.5 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label={t("prevPeriod")}
          disabled={!prevEnabled}
          onClick={() => shiftRange(-ROLLING_WINDOW_DAYS)}
        >
          ‹
        </button>
        <span className="min-w-[12rem] text-center text-sm font-semibold text-stay-navy">
          {displayLabel}
        </span>
        <button
          type="button"
          className="btn-ghost px-2.5"
          aria-label={t("nextPeriod")}
          onClick={() => shiftRange(ROLLING_WINDOW_DAYS)}
        >
          ›
        </button>
      </div>
      <button
        type="button"
        className="btn-ghost"
        disabled={atFloor}
        onClick={goToday}
      >
        {t("today")}
      </button>
      <label className="inline-flex items-center gap-1.5 text-sm text-stay-navy">
        <input
          type="checkbox"
          checked={historyEnabled}
          onChange={(e) => setHistory(e.target.checked)}
          aria-label={t("historyAria")}
          className="h-3.5 w-3.5 shrink-0 rounded border-stay-border text-stay-blue focus:ring-stay-blue"
        />
        <span>{t("history")}</span>
      </label>
    </>
  );
}
