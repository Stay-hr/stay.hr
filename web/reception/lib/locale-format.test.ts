import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";
import {
  formatStayDateRange,
  stayNightsCount,
} from "@/lib/locale-format";
import hr from "@/messages/hr.json";

describe("formatStayDateRange", () => {
  it("formats same month with year once at the end", () => {
    const label = formatStayDateRange("hr", "2026-08-08", "2026-08-11");
    expect(label).toMatch(/2026/);
    expect(label?.match(/2026/g)?.length).toBe(1);
    expect(label).toContain("→");
    expect(label).not.toContain("Invalid Date");
  });

  it("formats different month same year with year once", () => {
    const label = formatStayDateRange("hr", "2026-08-30", "2026-09-02");
    expect(label).toBeTruthy();
    expect(label?.match(/2026/g)?.length).toBe(1);
    expect(label).toMatch(/30/);
    expect(label).toMatch(/2/);
    expect(label).toContain("→");
    // HR short months: kol / ruj (or similar locale abbreviations)
    expect(label?.toLowerCase()).toMatch(/kol/);
    expect(label?.toLowerCase()).toMatch(/ruj/);
  });

  it("formats different year with year on both sides", () => {
    const label = formatStayDateRange("hr", "2026-12-30", "2027-01-02");
    expect(label).toContain("2026");
    expect(label).toContain("2027");
    expect(label).toContain("→");
  });

  it("never renders Invalid Date; falls back to raw strings", () => {
    expect(formatStayDateRange("hr", "not-a-date", "2026-08-11")).toBe(
      "not-a-date → 2026-08-11",
    );
    expect(formatStayDateRange("hr", "2026-08-08", "bogus")).toBe("2026-08-08 → bogus");
    expect(formatStayDateRange("hr", "2026-02-31", "2026-03-01")).toBe(
      "2026-02-31 → 2026-03-01",
    );
    expect(formatStayDateRange("hr", "", "")).toBeNull();
    expect(formatStayDateRange("hr", "bad", "")).toBe("bad");
  });
});

describe("stayNightsCount", () => {
  it("counts one night and multi-night stays", () => {
    expect(stayNightsCount("2026-08-08", "2026-08-09")).toBe(1);
    expect(stayNightsCount("2026-08-08", "2026-08-11")).toBe(3);
  });

  it("returns null for equal, inverted, or invalid ranges", () => {
    expect(stayNightsCount("2026-08-08", "2026-08-08")).toBeNull();
    expect(stayNightsCount("2026-08-11", "2026-08-08")).toBeNull();
    expect(stayNightsCount("bad", "2026-08-11")).toBeNull();
    expect(stayNightsCount("2026-02-31", "2026-03-01")).toBeNull();
    expect(stayNightsCount("", "")).toBeNull();
  });

  it("uses UTC noon date math (TZ/DST safe)", () => {
    // Same calendar nights regardless of local TZ interpretation of midnights.
    expect(stayNightsCount("2026-03-28", "2026-03-30")).toBe(2); // EU DST spring
    expect(stayNightsCount("2026-10-24", "2026-10-26")).toBe(2); // EU DST fall
  });
});

describe("HR plurals (common.nightsCount / guestsCount)", () => {
  const t = createTranslator({ locale: "hr", messages: hr, namespace: "common" });

  it("formats nightsCount", () => {
    expect(t("nightsCount", { count: 1 })).toBe("1 noć");
    expect(t("nightsCount", { count: 2 })).toBe("2 noći");
    expect(t("nightsCount", { count: 5 })).toBe("5 noći");
  });

  it("formats guestsCount", () => {
    expect(t("guestsCount", { count: 1 })).toBe("1 gost");
    expect(t("guestsCount", { count: 2 })).toBe("2 gosta");
    expect(t("guestsCount", { count: 5 })).toBe("5 gostiju");
  });
});
