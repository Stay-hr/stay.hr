"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import {
  billingRecipientFormFromRow,
  billingRecipientWritePayload,
  EMPTY_BILLING_RECIPIENT_FORM,
  isHrTaxCountry,
  validateBillingRecipientReady,
  type BillingRecipientFormValues,
  type OpenBillingRecipient,
} from "@/lib/billingRecipient";

type InvoiceDetailsPayload = {
  status?: string;
  writable?: boolean;
  kind?: "company" | "personal";
  property_name: string;
  check_in: string;
  check_out: string;
  guest_label?: string;
  booking_code?: string;
  personal_email?: string;
  recipient?: OpenBillingRecipient | null;
};

type Kind = "company" | "personal";

type Props = {
  token: string;
  lang?: string | null;
};

function formatStayDate(iso: string, locale: string): string {
  try {
    return new Intl.DateTimeFormat(locale === "hr" ? "hr-HR" : "en-GB", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

export function GuestInvoiceDetailsView({ token, lang }: Props) {
  const t = useTranslations("guestInvoiceDetails");
  const locale = lang || "hr";
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [gateStatus, setGateStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [payload, setPayload] = useState<InvoiceDetailsPayload | null>(null);
  const [kind, setKind] = useState<Kind>("company");
  const [personalEmail, setPersonalEmail] = useState("");
  const [values, setValues] = useState<BillingRecipientFormValues>(EMPTY_BILLING_RECIPIENT_FORM);

  function applyPayload(data: InvoiceDetailsPayload) {
    setPayload(data);
    const nextKind = data.kind === "personal" ? "personal" : "company";
    setKind(nextKind);
    setPersonalEmail(data.personal_email || "");
    if (data.recipient) {
      setValues(billingRecipientFormFromRow(data.recipient));
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const qs = lang ? `?lang=${encodeURIComponent(lang)}` : "";
        const res = await fetch(`/api/invoice-details/${encodeURIComponent(token)}${qs}`);
        const data = (await res.json()) as InvoiceDetailsPayload;
        if (cancelled) return;
        if (res.status === 410) {
          setGateStatus(data.status || "unavailable");
          setPayload(null);
          return;
        }
        if (!res.ok) {
          setError(t("loadFailed"));
          setPayload(null);
          return;
        }
        setGateStatus(data.status === "issued" ? "issued" : null);
        applyPayload(data);
      } catch {
        if (!cancelled) {
          setError(t("loadFailed"));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [token, lang, t]);

  function update<K extends keyof BillingRecipientFormValues>(
    name: K,
    value: BillingRecipientFormValues[K],
  ) {
    setValues((current) => ({ ...current, [name]: value }));
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (kind === "company") {
      const fieldError = validateBillingRecipientReady(values);
      if (fieldError) {
        setError(t(fieldError));
        return;
      }
    } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(personalEmail.trim())) {
      setError(t("emailInvalid"));
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const body =
        kind === "company"
          ? { kind: "company", ...billingRecipientWritePayload(values) }
          : { kind: "personal", email: personalEmail.trim() };
      const res = await fetch(`/api/invoice-details/${encodeURIComponent(token)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as
        | (InvoiceDetailsPayload & { detail?: string; reason?: string })
        | null;
      if (res.status === 410) {
        setGateStatus(data?.status || "issued");
        return;
      }
      if (!res.ok) {
        if (data?.reason === "structurally_incomplete") {
          throw new Error(t("incomplete"));
        }
        if (data?.reason === "email_not_usable") {
          throw new Error(t("emailInvalid"));
        }
        if (data?.reason === "company_form_in_progress") {
          throw new Error(t("companyInProgress"));
        }
        throw new Error(data?.detail || t("saveFailed"));
      }
      if (data) {
        applyPayload(data);
      }
      setMessage(t("saved"));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="card text-center">
        <p className="text-muted">{t("loading")}</p>
      </div>
    );
  }

  if (gateStatus === "expired" || gateStatus === "revoked" || gateStatus === "unavailable") {
    return (
      <div className="card space-y-3 text-center">
        <h1 className="text-xl font-bold text-stay-navy">{t("unavailableTitle")}</h1>
        <p className="text-muted">{t("unavailableBody")}</p>
      </div>
    );
  }

  if (!payload || error && !payload) {
    return (
      <div className="card text-center">
        <p className="text-red-600">{error || t("loadFailed")}</p>
      </div>
    );
  }

  const writable = Boolean(payload.writable) && gateStatus !== "issued";
  const hrTax = isHrTaxCountry(values.tax_id_country);
  const ready = payload.recipient?.status === "ready";

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold text-stay-navy">{payload.property_name}</h1>
        <p className="text-sm text-muted">{t("subtitle")}</p>
      </div>

      <section className="card space-y-4">
        <h2 className="text-lg font-semibold text-stay-navy">{t("stayTitle")}</h2>
        <dl className="grid gap-2 text-sm">
          <div>
            <dt className="text-muted">{t("checkIn")}</dt>
            <dd className="font-medium">{formatStayDate(payload.check_in, locale)}</dd>
          </div>
          <div>
            <dt className="text-muted">{t("checkOut")}</dt>
            <dd className="font-medium">{formatStayDate(payload.check_out, locale)}</dd>
          </div>
          {payload.guest_label ? (
            <div>
              <dt className="text-muted">{t("guest")}</dt>
              <dd className="font-medium">{payload.guest_label}</dd>
            </div>
          ) : null}
        </dl>
      </section>

      {gateStatus === "issued" ? (
        <section className="card space-y-2">
          <h2 className="text-lg font-semibold text-stay-navy">{t("issuedTitle")}</h2>
          <p className="text-sm text-muted">{t("issuedBody")}</p>
        </section>
      ) : null}

      {ready && writable ? (
        <p className="text-sm text-emerald-700">{t("readyHint")}</p>
      ) : null}

      <section className="card space-y-4">
        <h2 className="text-lg font-semibold text-stay-navy">{t("formTitle")}</h2>
        {message ? <p className="text-sm text-emerald-700">{message}</p> : null}
        {error ? <p className="text-sm text-red-600">{error}</p> : null}

        <div className="flex flex-wrap gap-3 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="invoice-kind"
              checked={kind === "company"}
              disabled={!writable}
              onChange={() => setKind("company")}
            />
            {t("kindCompany")}
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="invoice-kind"
              checked={kind === "personal"}
              disabled={!writable || Boolean(payload.recipient)}
              onChange={() => setKind("personal")}
            />
            {t("kindPersonal")}
          </label>
        </div>

        <form className="grid gap-3 sm:grid-cols-2" onSubmit={(event) => void onSubmit(event)}>
          {kind === "personal" ? (
            <label className="block text-sm sm:col-span-2">
              <span className="text-muted">{t("email")}</span>
              <input
                type="email"
                className="input mt-1"
                disabled={!writable}
                value={personalEmail}
                onChange={(event) => setPersonalEmail(event.target.value)}
              />
            </label>
          ) : (
            <>
              <label className="block text-sm sm:col-span-2">
                <span className="text-muted">{t("companyName")}</span>
                <input
                  className="input mt-1"
                  disabled={!writable}
                  value={values.company_name}
                  onChange={(event) => update("company_name", event.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("taxId")}</span>
                <input
                  className="input mt-1"
                  disabled={!writable}
                  inputMode={hrTax ? "numeric" : "text"}
                  maxLength={hrTax ? 11 : 64}
                  value={values.tax_id}
                  onChange={(event) => update("tax_id", event.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("taxIdCountry")}</span>
                <input
                  className="input mt-1 uppercase"
                  disabled={!writable}
                  maxLength={2}
                  autoComplete="off"
                  value={values.tax_id_country}
                  onChange={(event) => update("tax_id_country", event.target.value.toUpperCase())}
                />
              </label>
              <label className="block text-sm sm:col-span-2">
                <span className="text-muted">{t("address")}</span>
                <input
                  className="input mt-1"
                  disabled={!writable}
                  value={values.address}
                  onChange={(event) => update("address", event.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("postalCode")}</span>
                <input
                  className="input mt-1"
                  disabled={!writable}
                  value={values.postal_code}
                  onChange={(event) => update("postal_code", event.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("city")}</span>
                <input
                  className="input mt-1"
                  disabled={!writable}
                  value={values.city}
                  onChange={(event) => update("city", event.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("country")}</span>
                <input
                  className="input mt-1 uppercase"
                  disabled={!writable}
                  maxLength={2}
                  autoComplete="off"
                  value={values.country}
                  onChange={(event) => update("country", event.target.value.toUpperCase())}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("email")}</span>
                <input
                  type="email"
                  className="input mt-1"
                  disabled={!writable}
                  value={values.email}
                  onChange={(event) => update("email", event.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="text-muted">{t("phone")}</span>
                <input
                  className="input mt-1"
                  disabled={!writable}
                  value={values.phone}
                  onChange={(event) => update("phone", event.target.value)}
                />
              </label>
            </>
          )}
          {writable ? (
            <div className="sm:col-span-2">
              <button type="submit" className="btn btn-sm" disabled={saving}>
                {saving ? t("saving") : t("submit")}
              </button>
            </div>
          ) : null}
        </form>
      </section>
    </div>
  );
}
