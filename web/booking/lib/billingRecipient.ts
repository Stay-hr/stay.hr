export type BillingRecipientFormValues = {
  company_name: string;
  tax_id: string;
  tax_id_country: string;
  country: string;
  address: string;
  postal_code: string;
  city: string;
  email: string;
  phone: string;
};

export type OpenBillingRecipient = BillingRecipientFormValues & {
  id: number;
  reservation_id: number;
  status: "requested" | "ready" | "applied";
  identity_confidence: string;
  requested_at: string | null;
  ready_at: string | null;
};

export const EMPTY_BILLING_RECIPIENT_FORM: BillingRecipientFormValues = {
  company_name: "",
  tax_id: "",
  tax_id_country: "",
  country: "",
  address: "",
  postal_code: "",
  city: "",
  email: "",
  phone: "",
};

export function normalizeIso2(value: string): string {
  return value.trim().toUpperCase();
}

export function isValidIso2(value: string): boolean {
  return /^[A-Z]{2}$/.test(normalizeIso2(value));
}

export function isHrTaxCountry(value: string): boolean {
  return normalizeIso2(value) === "HR";
}

export function taxIdDigits(value: string): string {
  return value.replace(/\D/g, "");
}

export function billingRecipientFormFromRow(
  row: OpenBillingRecipient,
): BillingRecipientFormValues {
  return {
    company_name: row.company_name || "",
    tax_id: row.tax_id || "",
    tax_id_country: row.tax_id_country || "",
    country: row.country || "",
    address: row.address || "",
    postal_code: row.postal_code || "",
    city: row.city || "",
    email: row.email || "",
    phone: row.phone || "",
  };
}

export type BillingRecipientReadyError =
  | "incomplete"
  | "countryIso"
  | "hrTaxId"
  | "emailInvalid";

export function billingRecipientWritePayload(
  values: BillingRecipientFormValues,
): BillingRecipientFormValues {
  const taxCountry = normalizeIso2(values.tax_id_country);
  const taxId = values.tax_id.trim();
  return {
    company_name: values.company_name.trim(),
    tax_id: taxCountry === "HR" ? taxIdDigits(taxId) : taxId,
    tax_id_country: taxCountry,
    country: normalizeIso2(values.country),
    address: values.address.trim(),
    postal_code: values.postal_code.trim(),
    city: values.city.trim(),
    email: values.email.trim(),
    phone: values.phone.trim(),
  };
}

export function validateBillingRecipientReady(
  values: BillingRecipientFormValues,
): BillingRecipientReadyError | null {
  const payload = billingRecipientWritePayload(values);
  const required: Array<keyof BillingRecipientFormValues> = [
    "company_name",
    "tax_id",
    "tax_id_country",
    "country",
    "address",
    "postal_code",
    "city",
    "email",
  ];
  if (required.some((name) => !payload[name])) {
    return "incomplete";
  }
  if (!isValidIso2(payload.tax_id_country) || !isValidIso2(payload.country)) {
    return "countryIso";
  }
  if (isHrTaxCountry(payload.tax_id_country) && taxIdDigits(payload.tax_id).length !== 11) {
    return "hrTaxId";
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(payload.email)) {
    return "emailInvalid";
  }
  return null;
}
