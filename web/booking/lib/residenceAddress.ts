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

/**
 * PATCH address policy (fingerprint B fix):
 * - both empty → send "" (explicit clear)
 * - only city or only street → omit key (do not keep-previous / re-send Zagreb)
 * - both present → send composed "City, Street"
 */
export type AddressPatchDecision =
  | { kind: "omit" }
  | { kind: "set"; address: string };

export function addressPatchDecision(city: string, street: string): AddressPatchDecision {
  const c = city.trim();
  const s = street.trim();
  if (!c && !s) return { kind: "set", address: "" };
  if (!c || !s) return { kind: "omit" };
  return { kind: "set", address: `${c}, ${s}` };
}

/** Local form.address mirror: update only when PATCH would set; else leave previous. */
export function resolveLocalAddress(
  city: string,
  street: string,
  previousAddress: string,
): string {
  const decision = addressPatchDecision(city, street);
  if (decision.kind === "omit") return previousAddress;
  return decision.address;
}

/**
 * Build slot PATCH body. Omits `address` while residence fields are incomplete
 * so autosave cannot re-persist a stale Zagreb (or any previous) value.
 */
export function buildGuestPatchPayload<T extends { address: string }>(
  form: T,
  city: string,
  street: string,
): Omit<T, "address"> & { address?: string } {
  const { address: _drop, ...rest } = form;
  const decision = addressPatchDecision(city, street);
  if (decision.kind === "omit") {
    return rest;
  }
  return { ...rest, address: decision.address };
}

/**
 * @deprecated Prefer addressPatchDecision / buildGuestPatchPayload.
 * Kept for older tests; keep-previous on incomplete is the B-bug mechanism.
 */
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
