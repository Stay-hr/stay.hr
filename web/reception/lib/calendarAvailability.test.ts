import { describe, expect, it } from "vitest";
import { isDayTappable } from "@/lib/calendarAvailability";

describe("isDayTappable", () => {
  it("keeps yesterday read-only and today tappable", () => {
    expect(isDayTappable("2026-08-13", "2026-08-14")).toBe(false);
    expect(isDayTappable("2026-08-14", "2026-08-14")).toBe(true);
    expect(isDayTappable("2026-08-15", "2026-08-14")).toBe(true);
  });
});
