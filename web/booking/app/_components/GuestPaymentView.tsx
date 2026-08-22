"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

type PaymentPayload = {
  status?: string;
  property_name: string;
  check_in: string;
  check_out: string;
  unit_label?: string;
  guest_label?: string;
  payment_amount: string;
  currency: string;
  includes_tourist_tax?: boolean;
  iban?: string;
  beneficiary?: string;
  payment_reference?: string;
  payment_note?: string;
};

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

export function GuestPaymentView({ token, lang }: Props) {
  const t = useTranslations("guestPayment");
  const locale = lang || "hr";
  const [loading, setLoading] = useState(true);
  const [gateStatus, setGateStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [payload, setPayload] = useState<PaymentPayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const qs = lang ? `?lang=${encodeURIComponent(lang)}` : "";
        const res = await fetch(`/api/pay/${encodeURIComponent(token)}${qs}`);
        const data = (await res.json()) as PaymentPayload;
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
        setGateStatus(null);
        setPayload(data);
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

  if (loading) {
    return (
      <div className="card text-center">
        <p className="text-muted">{t("loading")}</p>
      </div>
    );
  }

  if (
    gateStatus === "expired" ||
    gateStatus === "revoked" ||
    gateStatus === "unavailable"
  ) {
    return (
      <div className="card space-y-3 text-center">
        <h1 className="text-xl font-bold text-stay-navy">{t("unavailableTitle")}</h1>
        <p className="text-muted">{t("unavailableBody")}</p>
      </div>
    );
  }

  if (!payload || error) {
    return (
      <div className="card text-center">
        <p className="text-red-600">{error || t("loadFailed")}</p>
      </div>
    );
  }

  const currency = payload.currency || "EUR";
  const amountLabel = `${payload.payment_amount} ${currency}`;

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
          {payload.unit_label ? (
            <div>
              <dt className="text-muted">{t("unit")}</dt>
              <dd className="font-medium">{payload.unit_label}</dd>
            </div>
          ) : null}
          {payload.guest_label ? (
            <div>
              <dt className="text-muted">{t("guest")}</dt>
              <dd className="font-medium">{payload.guest_label}</dd>
            </div>
          ) : null}
        </dl>
      </section>

      <section className="card space-y-4">
        <h2 className="text-lg font-semibold text-stay-navy">{t("paymentTitle")}</h2>
        <p className="text-2xl font-bold text-stay-navy">{amountLabel}</p>
        {payload.includes_tourist_tax ? (
          <p className="text-sm text-muted">{t("includesTouristTax")}</p>
        ) : null}
        <dl className="grid gap-2 text-sm">
          {payload.beneficiary ? (
            <div>
              <dt className="text-muted">{t("beneficiary")}</dt>
              <dd className="font-medium break-words">{payload.beneficiary}</dd>
            </div>
          ) : null}
          {payload.iban ? (
            <div>
              <dt className="text-muted">{t("iban")}</dt>
              <dd className="font-mono text-base font-medium tracking-wide break-all">
                {payload.iban}
              </dd>
            </div>
          ) : null}
          {payload.payment_reference ? (
            <div>
              <dt className="text-muted">{t("paymentReference")}</dt>
              <dd className="font-mono font-medium break-all">{payload.payment_reference}</dd>
            </div>
          ) : null}
          {payload.payment_note ? (
            <div>
              <dt className="text-muted">{t("paymentNote")}</dt>
              <dd className="font-medium">{payload.payment_note}</dd>
            </div>
          ) : null}
        </dl>
        <p className="text-sm text-muted">{t("footerHint")}</p>
      </section>
    </div>
  );
}
