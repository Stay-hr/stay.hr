import { describe, expect, it } from "vitest";
import {
  billingRecipientWritePayload,
  isHrTaxCountry,
  validateBillingRecipientForm,
} from "@/lib/billingRecipient";

const readyValues = {
  company_name: "Example GmbH",
  tax_id: "DE123456789",
  tax_id_country: "de",
  country: "de",
  address: "Unter den Linden 1",
  postal_code: "10115",
  city: "Berlin",
  email: "billing@example.com",
  phone: "",
};

describe("validateBillingRecipientForm", () => {
  it("rejects an empty request", () => {
    expect(
      validateBillingRecipientForm({
        ...readyValues,
        company_name: "",
        tax_id: "",
      }),
    ).toBe("empty");
  });

  it("requires 11 digits for a Croatian OIB", () => {
    expect(
      validateBillingRecipientForm({
        ...readyValues,
        tax_id: "1234567890",
        tax_id_country: "HR",
      }),
    ).toBe("hrTaxId");
    expect(
      validateBillingRecipientForm({
        ...readyValues,
        tax_id: "12345678901",
        tax_id_country: "hr",
      }),
    ).toBeNull();
  });

  it("does not cap a foreign tax id at 11 characters", () => {
    expect(validateBillingRecipientForm(readyValues)).toBeNull();
    expect(
      validateBillingRecipientForm({
        ...readyValues,
        tax_id: "DE1234567890123",
      }),
    ).toBeNull();
  });
});

describe("billingRecipientWritePayload", () => {
  it("normalizes ISO2 and never includes status or identity_confidence", () => {
    const payload = billingRecipientWritePayload(readyValues);
    expect(payload.tax_id_country).toBe("DE");
    expect(payload.country).toBe("DE");
    expect(payload).not.toHaveProperty("status");
    expect(payload).not.toHaveProperty("identity_confidence");
    expect(Object.keys(payload).sort()).toEqual(
      [
        "address",
        "city",
        "company_name",
        "country",
        "email",
        "phone",
        "postal_code",
        "tax_id",
        "tax_id_country",
      ].sort(),
    );
  });

  it("keeps only digits for a Croatian OIB", () => {
    expect(isHrTaxCountry("hr")).toBe(true);
    expect(
      billingRecipientWritePayload({
        ...readyValues,
        tax_id: "123-45678-901",
        tax_id_country: "HR",
      }).tax_id,
    ).toBe("12345678901");
  });
});
