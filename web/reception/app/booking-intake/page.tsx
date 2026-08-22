"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { singlePropertySlug } from "@/lib/app-config";
import { isValidBookerPhone, sanitizePhoneInput } from "@/lib/phoneInput";
import type { AppConfig } from "@/lib/types";

type IntakeDraft = {
  id: number;
  status: string;
  raw_text: string;
  missing_fields: string[];
  property_slug: string;
  unit_id: number | null;
  unit_code: string;
  check_in: string | null;
  check_out: string | null;
  amount: string | null;
  currency: string;
  booker_name: string;
  booker_phone: string;
  booker_email: string;
  booker_address: string;
  buyer_company_name: string;
  buyer_oib: string;
  buyer_address: string;
  invoice_email: string;
  guest_first_name: string;
  guest_last_name: string;
};

export default function BookingIntakePage() {
  const router = useRouter();
  const t = useTranslations("bookingIntake");
  const tc = useTranslations("common");
  const [tenantName, setTenantName] = useState("");
  const [featureFlags, setFeatureFlags] = useState<AppConfig["feature_flags"]>();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [rawText, setRawText] = useState("");
  const [draftId, setDraftId] = useState<number | null>(null);
  const [missingFields, setMissingFields] = useState<string[]>([]);

  const [propertySlug, setPropertySlug] = useState("");
  const [unitId, setUnitId] = useState("");
  const [checkIn, setCheckIn] = useState("");
  const [checkOut, setCheckOut] = useState("");
  const [amount, setAmount] = useState("");
  const [bookerName, setBookerName] = useState("");
  const [bookerPhone, setBookerPhone] = useState("");
  const [bookerEmail, setBookerEmail] = useState("");
  const [bookerAddress, setBookerAddress] = useState("");
  const [buyerCompanyName, setBuyerCompanyName] = useState("");
  const [buyerOib, setBuyerOib] = useState("");
  const [buyerAddress, setBuyerAddress] = useState("");
  const [invoiceEmail, setInvoiceEmail] = useState("");
  const [guestFirstName, setGuestFirstName] = useState("");
  const [guestLastName, setGuestLastName] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const session = await fetch("/api/auth/session");
      if (session.ok) {
        const s = await session.json();
        setTenantName(s.tenant || "");
      }
      const configRes = await fetch("/api/stay/app/config");
      if (!configRes.ok) throw new Error(t("loadConfigFailed"));
      const appConfig = (await configRes.json()) as AppConfig;
      setConfig(appConfig);
      setFeatureFlags(appConfig.feature_flags);
      setPropertySlug((current) => {
        if (current) return current;
        const single = singlePropertySlug(appConfig);
        if (single) return single;
        const uzorita = appConfig.properties?.find((property) => property.slug === "uzorita");
        return uzorita?.slug ?? "";
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      setLoading(false);
    }
  }, [t, tc]);

  useEffect(() => {
    void load();
  }, [load]);

  const properties = config?.properties ?? [];
  const units = useMemo(() => {
    const all = config?.units ?? [];
    if (!propertySlug) return all;
    return all.filter((unit) => !unit.property_slug || unit.property_slug === propertySlug);
  }, [config, propertySlug]);

  function applyDraft(draft: IntakeDraft) {
    setDraftId(draft.id);
    setMissingFields(draft.missing_fields || []);
    if (draft.property_slug) setPropertySlug(draft.property_slug);
    if (draft.unit_id) setUnitId(String(draft.unit_id));
    else if (draft.unit_code) {
      const match = (config?.units ?? []).find(
        (unit) => unit.code?.toUpperCase() === draft.unit_code.toUpperCase()
      );
      if (match) setUnitId(String(match.id));
    }
    setCheckIn(draft.check_in || "");
    setCheckOut(draft.check_out || "");
    setAmount(draft.amount || "");
    setBookerName(draft.booker_name || "");
    setBookerPhone(draft.booker_phone || "");
    setBookerEmail(draft.booker_email || "");
    setBookerAddress(draft.booker_address || "");
    setBuyerCompanyName(draft.buyer_company_name || "");
    setBuyerOib(draft.buyer_oib || "");
    setBuyerAddress(draft.buyer_address || "");
    setInvoiceEmail(draft.invoice_email || "");
    setGuestFirstName(draft.guest_first_name || "");
    setGuestLastName(draft.guest_last_name || "");
  }

  async function handleParse() {
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/stay/reception/booking-intake/parse/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          raw_text: rawText,
          property_slug: propertySlug || undefined,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || t("parseFailed"));
      }
      applyDraft(data as IntakeDraft);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("parseFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm(e: FormEvent) {
    e.preventDefault();
    if (!draftId) {
      setError(t("parseFirst"));
      return;
    }
    if (bookerPhone && !isValidBookerPhone(bookerPhone)) {
      setError(t("phoneInvalid"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/stay/reception/booking-intake/confirm/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft_id: draftId,
          property_slug: propertySlug,
          unit_id: Number(unitId),
          check_in: checkIn,
          check_out: checkOut,
          booker_name: bookerName,
          booker_phone: bookerPhone,
          booker_email: bookerEmail,
          booker_address: bookerAddress,
          amount: amount || null,
          buyer_company_name: buyerCompanyName,
          buyer_oib: buyerOib,
          buyer_address: buyerAddress,
          invoice_email: invoiceEmail,
          guest_first_name: guestFirstName,
          guest_last_name: guestLastName,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || t("confirmFailed"));
      }
      const reservationId = data.reservation?.id;
      if (reservationId) {
        router.push(`/reservations/${reservationId}`);
        router.refresh();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("confirmFailed"));
    } finally {
      setBusy(false);
    }
  }

  if (!loading && featureFlags && !featureFlags.reception_booking_intake) {
    return (
      <div>
        <ReceptionNav tenantName={tenantName} featureFlags={featureFlags} />
        <main className="mx-auto max-w-6xl px-4 py-6">
          <p className="text-muted">{t("notAvailable")}</p>
        </main>
      </div>
    );
  }

  return (
    <div>
      <ReceptionNav tenantName={tenantName} featureFlags={featureFlags} />
      <main className="mx-auto max-w-2xl space-y-4 px-4 py-6">
        <h1 className="text-xl font-bold text-stay-navy">{t("title")}</h1>
        <p className="text-sm text-muted">{t("subtitle")}</p>
        {loading ? <p className="text-muted">{tc("loading")}</p> : null}
        {error ? <p className="text-sm text-red-600">{error}</p> : null}

        <div className="card space-y-3 p-4">
          <label className="block text-sm">
            <span className="label">{t("rawText")}</span>
            <textarea
              className="input mt-1 min-h-[160px]"
              value={rawText}
              onChange={(e) => setRawText(e.target.value)}
              placeholder={t("rawTextPlaceholder")}
            />
          </label>
          <button
            type="button"
            className="btn"
            disabled={busy || !rawText.trim()}
            onClick={() => void handleParse()}
          >
            {busy ? tc("loading") : t("parse")}
          </button>
          {draftId ? (
            <p className="text-xs text-muted">
              {t("draftId", { id: draftId })}
              {missingFields.length ? ` · ${t("missing")}: ${missingFields.join(", ")}` : ""}
            </p>
          ) : null}
        </div>

        {draftId ? (
          <form className="card space-y-3 p-4" onSubmit={(e) => void handleConfirm(e)}>
            {properties.length > 1 ? (
              <label className="block text-sm">
                <span className="label">{t("property")}</span>
                <select
                  className="input mt-1"
                  value={propertySlug}
                  onChange={(e) => {
                    setPropertySlug(e.target.value);
                    setUnitId("");
                  }}
                  required
                >
                  <option value="">{t("propertyPlaceholder")}</option>
                  {properties.map((property) => (
                    <option key={property.slug} value={property.slug}>
                      {property.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            <label className="block text-sm">
              <span className="label">{t("unit")}</span>
              <select
                className="input mt-1"
                value={unitId}
                onChange={(e) => setUnitId(e.target.value)}
                required
              >
                <option value="">{t("unitPlaceholder")}</option>
                {units.map((unit) => (
                  <option key={unit.id} value={unit.id}>
                    {unit.code}
                  </option>
                ))}
              </select>
            </label>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-sm">
                <span className="label">{t("checkIn")}</span>
                <input
                  type="date"
                  className="input mt-1"
                  value={checkIn}
                  onChange={(e) => setCheckIn(e.target.value)}
                  required
                />
              </label>
              <label className="block text-sm">
                <span className="label">{t("checkOut")}</span>
                <input
                  type="date"
                  className="input mt-1"
                  value={checkOut}
                  onChange={(e) => setCheckOut(e.target.value)}
                  required
                />
              </label>
            </div>

            <label className="block text-sm">
              <span className="label">{t("amount")}</span>
              <input
                className="input mt-1"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                inputMode="decimal"
                placeholder="400.00"
              />
              <span className="mt-1 block text-xs text-muted">{t("amountHint")}</span>
            </label>

            <label className="block text-sm">
              <span className="label">{t("bookerName")}</span>
              <input
                className="input mt-1"
                value={bookerName}
                onChange={(e) => setBookerName(e.target.value)}
                required
              />
            </label>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-sm">
                <span className="label">{t("bookerPhone")}</span>
                <input
                  className="input mt-1"
                  value={bookerPhone}
                  onChange={(e) => setBookerPhone(sanitizePhoneInput(e.target.value))}
                />
              </label>
              <label className="block text-sm">
                <span className="label">{t("bookerEmail")}</span>
                <input
                  type="email"
                  className="input mt-1"
                  value={bookerEmail}
                  onChange={(e) => setBookerEmail(e.target.value)}
                />
              </label>
            </div>

            <label className="block text-sm">
              <span className="label">{t("bookerAddress")}</span>
              <input
                className="input mt-1"
                value={bookerAddress}
                onChange={(e) => setBookerAddress(e.target.value)}
              />
            </label>

            <fieldset className="space-y-3 border-t border-stay-border pt-3">
              <legend className="text-sm font-medium text-stay-navy">{t("companySection")}</legend>
              <label className="block text-sm">
                <span className="label">{t("buyerCompanyName")}</span>
                <input
                  className="input mt-1"
                  value={buyerCompanyName}
                  onChange={(e) => setBuyerCompanyName(e.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="label">{t("buyerOib")}</span>
                <input
                  className="input mt-1"
                  value={buyerOib}
                  onChange={(e) => setBuyerOib(e.target.value)}
                  maxLength={11}
                />
              </label>
              <label className="block text-sm">
                <span className="label">{t("buyerAddress")}</span>
                <input
                  className="input mt-1"
                  value={buyerAddress}
                  onChange={(e) => setBuyerAddress(e.target.value)}
                />
              </label>
              <label className="block text-sm">
                <span className="label">{t("invoiceEmail")}</span>
                <input
                  type="email"
                  className="input mt-1"
                  value={invoiceEmail}
                  onChange={(e) => setInvoiceEmail(e.target.value)}
                />
              </label>
            </fieldset>

            <fieldset className="space-y-3 border-t border-stay-border pt-3">
              <legend className="text-sm font-medium text-stay-navy">{t("guestSection")}</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm">
                  <span className="label">{t("guestFirstName")}</span>
                  <input
                    className="input mt-1"
                    value={guestFirstName}
                    onChange={(e) => setGuestFirstName(e.target.value)}
                  />
                </label>
                <label className="block text-sm">
                  <span className="label">{t("guestLastName")}</span>
                  <input
                    className="input mt-1"
                    value={guestLastName}
                    onChange={(e) => setGuestLastName(e.target.value)}
                  />
                </label>
              </div>
            </fieldset>

            <button type="submit" className="btn w-full sm:w-auto" disabled={busy}>
              {busy ? tc("loading") : t("confirm")}
            </button>
          </form>
        ) : null}
      </main>
    </div>
  );
}
