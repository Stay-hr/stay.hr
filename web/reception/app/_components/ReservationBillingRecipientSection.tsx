"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import {
  billingRecipientFormFromRow,
  billingRecipientWritePayload,
  EMPTY_BILLING_RECIPIENT_FORM,
  isHrTaxCountry,
  validateBillingRecipientForm,
  type BillingRecipientFormValues,
  type BillingRecipientStatus,
  type OpenBillingRecipient,
} from "@/lib/billingRecipient";
import { reservationBillingRecipientPath } from "@/lib/stay-client";

type Props = {
  reservationId: number;
};

function statusLabelKey(status: BillingRecipientStatus): "statusRequested" | "statusReady" {
  return status === "ready" ? "statusReady" : "statusRequested";
}

export function ReservationBillingRecipientSection({ reservationId }: Props) {
  const t = useTranslations("reservation.billingRecipient");
  const tc = useTranslations("common");
  const [values, setValues] = useState<BillingRecipientFormValues>(EMPTY_BILLING_RECIPIENT_FORM);
  const [existing, setExisting] = useState<OpenBillingRecipient | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(reservationBillingRecipientPath(reservationId));
      if (res.status === 404) {
        setExisting(null);
        setValues(EMPTY_BILLING_RECIPIENT_FORM);
        return;
      }
      if (!res.ok) {
        throw new Error(t("loadFailed"));
      }
      const row = (await res.json()) as OpenBillingRecipient;
      setExisting(row);
      setValues(billingRecipientFormFromRow(row));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [reservationId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  function update<K extends keyof BillingRecipientFormValues>(
    name: K,
    value: BillingRecipientFormValues[K],
  ) {
    setValues((current) => ({ ...current, [name]: value }));
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const fieldError = validateBillingRecipientForm(values);
    if (fieldError) {
      setMessage("");
      setError(t(fieldError));
      return;
    }
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const payload = billingRecipientWritePayload(values);
      const res = await fetch(reservationBillingRecipientPath(reservationId), {
        method: existing ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = (await res.json().catch(() => null)) as
        | (OpenBillingRecipient & { detail?: string; reason?: string })
        | null;
      if (!res.ok) {
        if (data?.reason === "empty_request") {
          throw new Error(t("empty"));
        }
        if (data?.reason === "invalid_country") {
          throw new Error(t("countryIso"));
        }
        throw new Error(data?.detail || t("saveFailed"));
      }
      if (data && typeof data.id === "number") {
        setExisting(data);
        setValues(billingRecipientFormFromRow(data));
      }
      setMessage(t("saved"));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  const hrTax = isHrTaxCountry(values.tax_id_country);
  const status = existing?.status;

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">{t("title")}</h2>
        {status ? (
          <span
            className={`badge ${
              status === "ready" ? "bg-emerald-100 text-emerald-800" : "badge-expected"
            }`}
          >
            {t(statusLabelKey(status))}
          </span>
        ) : null}
      </div>

      {loading ? <p className="text-sm text-muted">{tc("loading")}</p> : null}
      {message ? <p className="text-sm text-emerald-700">{message}</p> : null}
      {error ? <p className="text-sm text-red-600">{error}</p> : null}

      {loading ? null : (
        <form className="grid gap-3 sm:grid-cols-2" onSubmit={(event) => void onSubmit(event)}>
          <label className="block text-sm sm:col-span-2">
            <span className="label">{t("companyName")}</span>
            <input
              className="input mt-1"
              value={values.company_name}
              onChange={(event) => update("company_name", event.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("taxId")}</span>
            <input
              className="input mt-1"
              inputMode={hrTax ? "numeric" : "text"}
              maxLength={hrTax ? 11 : 64}
              value={values.tax_id}
              onChange={(event) => update("tax_id", event.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("taxIdCountry")}</span>
            <input
              className="input mt-1 uppercase"
              maxLength={2}
              autoComplete="off"
              value={values.tax_id_country}
              onChange={(event) => update("tax_id_country", event.target.value.toUpperCase())}
            />
          </label>
          <label className="block text-sm sm:col-span-2">
            <span className="label">{t("address")}</span>
            <input
              className="input mt-1"
              value={values.address}
              onChange={(event) => update("address", event.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("postalCode")}</span>
            <input
              className="input mt-1"
              value={values.postal_code}
              onChange={(event) => update("postal_code", event.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("city")}</span>
            <input
              className="input mt-1"
              value={values.city}
              onChange={(event) => update("city", event.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("country")}</span>
            <input
              className="input mt-1 uppercase"
              maxLength={2}
              autoComplete="off"
              value={values.country}
              onChange={(event) => update("country", event.target.value.toUpperCase())}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("email")}</span>
            <input
              type="email"
              className="input mt-1"
              value={values.email}
              onChange={(event) => update("email", event.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="label">{t("phone")}</span>
            <input
              className="input mt-1"
              value={values.phone}
              onChange={(event) => update("phone", event.target.value)}
            />
          </label>
          <div className="sm:col-span-2">
            <button type="submit" className="btn btn-sm" disabled={saving}>
              {saving ? tc("loading") : t("save")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
