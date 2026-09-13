import { describe, expect, it } from "vitest";
import {
  billingRecipientWritePayload,
  validateBillingRecipientReady,
} from "@/lib/billingRecipient";

const readyValues = {
  company_name: "Julianna Pihlar S.P.",
  tax_id: "96977604",
  tax_id_country: "si",
  country: "si",
  address: "Ljubljanska 1",
  postal_code: "1000",
  city: "Ljubljana",
  email: "julianna@edes.si",
  phone: "",
};

describe("validateBillingRecipientReady", () => {
  it("accepts a structurally complete SI company", () => {
    expect(validateBillingRecipientReady(readyValues)).toBeNull();
    expect(billingRecipientWritePayload(readyValues).tax_id_country).toBe("SI");
  });

  it("rejects missing address fields", () => {
    expect(validateBillingRecipientReady({ ...readyValues, address: "" })).toBe("incomplete");
    expect(validateBillingRecipientReady({ ...readyValues, city: "" })).toBe("incomplete");
  });

  it("requires 11 digits for a Croatian OIB", () => {
    expect(
      validateBillingRecipientReady({
        ...readyValues,
        tax_id: "123",
        tax_id_country: "HR",
        country: "HR",
      }),
    ).toBe("hrTaxId");
  });
});
