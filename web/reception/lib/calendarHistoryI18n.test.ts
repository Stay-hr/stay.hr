import { describe, expect, it } from "vitest";
import { locales, type AppLocale } from "@/i18n/locale";
import de from "@/messages/de.json";
import en from "@/messages/en.json";
import es from "@/messages/es.json";
import fr from "@/messages/fr.json";
import hr from "@/messages/hr.json";
import itMessages from "@/messages/it.json";

const catalogs: Record<AppLocale, { calendar: Record<string, unknown> }> = {
  de,
  en,
  es,
  fr,
  hr,
  it: itMessages,
};

const requiredKeys = ["history", "historyAria", "historyRangePrefix"] as const;

describe("calendar history i18n", () => {
  it("has non-empty history keys in all six locales", () => {
    for (const locale of locales) {
      const calendar = catalogs[locale].calendar;
      for (const key of requiredKeys) {
        const value = calendar[key];
        expect(typeof value, `${locale}.calendar.${key}`).toBe("string");
        expect(String(value).trim().length, `${locale}.calendar.${key}`).toBeGreaterThan(0);
      }
    }
  });
});
