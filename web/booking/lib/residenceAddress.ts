/**
 * Presentation helpers for guest check-in residence address split fields.
 * Backend residence_address.py remains the authority for normalization.
 */

export function composeResidenceAddress(city: string, street: string): string {
  const c = city.trim();
  const s = street.trim();
  if (!c || !s) return "";
  return `${c}, ${s}`;
}

export function splitResidenceAddress(raw: string): { city: string; street: string } {
  const value = (raw || "").trim();
  if (!value) return { city: "", street: "" };
  const idx = value.indexOf(",");
  if (idx === -1) return { city: "", street: value };
  return {
    city: value.slice(0, idx).trim(),
    street: value.slice(idx + 1).trim(),
  };
}

/** Resolve address for PATCH: complete compose, clear if both empty, else keep previous. */
export function resolveAddressForSave(
  city: string,
  street: string,
  previousAddress: string,
): string {
  const composed = composeResidenceAddress(city, street);
  if (composed) return composed;
  if (!city.trim() && !street.trim()) return "";
  return previousAddress;
}
