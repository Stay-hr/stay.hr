import { describe, expect, it } from "vitest";
import { addDaysIso } from "@/lib/utils";
import {
  canGoPrev,
  clampRangeStart,
  HISTORY_LOOKBACK_DAYS,
  historyMinStartIso,
  operationalFloorIso,
  shouldShowHistoryPrefix,
} from "@/lib/calendarLayout";

const TODAY = "2026-08-14";
const FLOOR = "2026-08-13";
const HISTORY_MIN = "2025-08-13";

describe("calendar history range helpers", () => {
  it("uses yesterday as the operational floor", () => {
    expect(operationalFloorIso(TODAY)).toBe(FLOOR);
  });

  it("sets history min to exactly 365 days before the floor", () => {
    expect(HISTORY_LOOKBACK_DAYS).toBe(365);
    expect(historyMinStartIso(FLOOR)).toBe(HISTORY_MIN);
    expect(addDaysIso(HISTORY_MIN, HISTORY_LOOKBACK_DAYS)).toBe(FLOOR);
  });

  it("clamps a -30 day step near the history boundary onto historyMin", () => {
    const nearBoundary = addDaysIso(HISTORY_MIN, 10);
    const rawPrev = addDaysIso(nearBoundary, -30);
    expect(rawPrev < HISTORY_MIN).toBe(true);
    expect(clampRangeStart(rawPrev, HISTORY_MIN)).toBe(HISTORY_MIN);
    expect(clampRangeStart(HISTORY_MIN, HISTORY_MIN)).toBe(HISTORY_MIN);
  });

  it("never clamps below minStart", () => {
    expect(clampRangeStart("2020-01-01", HISTORY_MIN)).toBe(HISTORY_MIN);
    expect(clampRangeStart("2026-09-01", HISTORY_MIN)).toBe("2026-09-01");
  });

  it("disables prev on the floor when history is off", () => {
    expect(canGoPrev(FLOOR, FLOOR)).toBe(false);
    expect(canGoPrev(addDaysIso(FLOOR, 30), FLOOR)).toBe(true);
  });

  it("disables prev on historyMin when history is on", () => {
    expect(canGoPrev(HISTORY_MIN, HISTORY_MIN)).toBe(false);
    expect(canGoPrev(addDaysIso(HISTORY_MIN, 1), HISTORY_MIN)).toBe(true);
  });

  it("uses day arithmetic across a leap-year floor, not calendar months", () => {
    expect(historyMinStartIso("2024-02-29")).toBe("2023-03-01");
  });

  it("shows the history prefix only when the range is before the floor", () => {
    expect(shouldShowHistoryPrefix(FLOOR, FLOOR)).toBe(false);
    expect(shouldShowHistoryPrefix(addDaysIso(FLOOR, 30), FLOOR)).toBe(false);
    expect(shouldShowHistoryPrefix(addDaysIso(FLOOR, -30), FLOOR)).toBe(true);
  });
});
