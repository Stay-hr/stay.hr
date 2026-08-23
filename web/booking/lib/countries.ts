import countriesData from "../../../data/iso3166-countries.json";

export type CountryRecord = {
  iso2: string;
  iso3: string;
  name_en: string;
  name_hr: string;
};

export const COUNTRIES: CountryRecord[] = countriesData as CountryRecord[];

export function countryLabel(country: CountryRecord, locale: string): string {
  return locale.startsWith("hr") ? country.name_hr : country.name_en;
}

export function countryByIso2(iso2: string): CountryRecord | undefined {
  const code = (iso2 || "").trim().toUpperCase();
  return COUNTRIES.find((c) => c.iso2 === code);
}

export function filterCountries(query: string, locale: string): CountryRecord[] {
  const q = (query || "").trim().toLowerCase();
  if (!q) return COUNTRIES;
  return COUNTRIES.filter((country) => {
    const label = countryLabel(country, locale).toLowerCase();
    return (
      label.includes(q) ||
      country.name_en.toLowerCase().includes(q) ||
      country.name_hr.toLowerCase().includes(q) ||
      country.iso2.toLowerCase().includes(q) ||
      country.iso3.toLowerCase().includes(q)
    );
  });
}
