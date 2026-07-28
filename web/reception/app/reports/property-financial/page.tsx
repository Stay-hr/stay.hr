"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { ReportsSubNav } from "@/app/_components/ReportsSubNav";
import { PdvsSection } from "@/app/reports/property-financial/_components/PdvsSection";
import { singlePropertySlug } from "@/lib/app-config";
import {
  displayBookingReference,
  displayExternalReference,
  parsePropertyFinancialReportError,
  propertyFinancialReportPath,
  sendPropertyFinancialReportEmail,
  type PropertyFinancialReport,
  type PropertyFinancialReportError,
} from "@/lib/propertyFinancialReport";
import { formatReservationAmount } from "@/lib/reservationFinance";
import type { AppConfig } from "@/lib/types";
import { addDaysIso, addMonthsIso, startOfMonthIso, todayIso } from "@/lib/utils";

function endOfMonthIso(monthStartIso: string): string {
  return addDaysIso(addMonthsIso(monthStartIso, 1), -1);
}

function defaultPreviousMonthPeriod(): { from: string; to: string } {
  const thisMonthStart = startOfMonthIso(todayIso());
  const from = addMonthsIso(thisMonthStart, -1);
  return { from, to: endOfMonthIso(from) };
}

function defaultThisMonthPeriod(): { from: string; to: string } {
  const from = startOfMonthIso(todayIso());
  return { from, to: endOfMonthIso(from) };
}

export default function PropertyFinancialReportPage() {
  const t = useTranslations("propertyFinancialReport");
  const tc = useTranslations("common");
  const locale = useLocale();
  const initialPeriod = useMemo(() => defaultPreviousMonthPeriod(), []);

  const [tenantName, setTenantName] = useState("");
  const [properties, setProperties] = useState<
    Array<{ slug: string; name: string; financial_report_recipients?: string }>
  >([]);
  const [propertySlug, setPropertySlug] = useState("");
  const [checkOutFrom, setCheckOutFrom] = useState(initialPeriod.from);
  const [checkOutTo, setCheckOutTo] = useState(initialPeriod.to);
  const [emailRecipients, setEmailRecipients] = useState("");
  const [report, setReport] = useState<PropertyFinancialReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [sendingEmail, setSendingEmail] = useState(false);
  const [emailSuccess, setEmailSuccess] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const session = await fetch("/api/auth/session");
        if (session.ok) {
          const data = await session.json();
          setTenantName(data.tenant || "");
        }

        const configRes = await fetch("/api/stay/app/config");
        if (!configRes.ok) return;
        const config = (await configRes.json()) as AppConfig;
        const nextProperties = config.properties ?? [];
        setProperties(nextProperties);
        setPropertySlug((current) => {
          if (current && nextProperties.some((property) => property.slug === current)) {
            return current;
          }
          const single = singlePropertySlug(config);
          if (single) return single;
          const uzorita = nextProperties.find((property) => property.slug === "uzorita");
          return uzorita?.slug ?? nextProperties[0]?.slug ?? "";
        });
      } catch {
        // ignore bootstrap errors; load action will surface them
      }
    })();
  }, []);

  const selectedProperty = useMemo(
    () => properties.find((property) => property.slug === propertySlug),
    [properties, propertySlug],
  );

  useEffect(() => {
    setEmailRecipients(selectedProperty?.financial_report_recipients?.trim() ?? "");
    setEmailSuccess("");
  }, [selectedProperty?.financial_report_recipients, propertySlug]);

  const parseRecipientInput = useCallback((raw: string) => {
    return raw
      .split(/[,;\s]+/)
      .map((part) => part.trim())
      .filter(Boolean);
  }, []);

  const formatEmailError = useCallback(
    (parsed: PropertyFinancialReportError | null) => {
      if (parsed?.code === "no_recipient") return t("emailNoRecipients");
      if (parsed?.code === "no_smtp") return t("errors.emailNoSmtp");
      if (parsed?.code === "no_from_address") return t("errors.emailNoFrom");
      if (parsed?.code === "period_too_long") {
        return t("errors.periodTooLong", { maxDays: parsed.max_days ?? 90 });
      }
      if (parsed?.code === "period_invalid") return t("errors.periodInvalid");
      if (parsed?.code === "property_required") {
        return parsed.detail || t("errors.propertyRequired");
      }
      return t("errors.emailSendFailed");
    },
    [t],
  );

  const formatAmount = useCallback(
    (value: string | null | undefined) => {
      if (value == null || value === "") return tc("dash");
      return `${formatReservationAmount(value, locale)} ${report?.meta.currency ?? "EUR"}`;
    },
    [locale, report?.meta.currency, tc],
  );

  const loadReport = useCallback(async () => {
    if (!propertySlug) {
      setError(t("errors.propertyRequired"));
      setReport(null);
      return;
    }
    if (!checkOutFrom || !checkOutTo) {
      setError(t("errors.periodInvalid"));
      setReport(null);
      return;
    }

    setLoading(true);
    setError("");
    try {
      const url = propertyFinancialReportPath({
        propertySlug,
        checkOutFrom,
        checkOutTo,
      });
      const res = await fetch(url);
      const payload = await res.json().catch(() => null);
      if (!res.ok) {
        const parsed = parsePropertyFinancialReportError(payload);
        if (parsed?.code === "period_too_long") {
          setError(t("errors.periodTooLong", { maxDays: parsed.max_days ?? 90 }));
        } else if (parsed?.code === "property_required") {
          setError(parsed.detail || t("errors.propertyRequired"));
        } else if (parsed?.code === "period_invalid") {
          setError(t("errors.periodInvalid"));
        } else {
          setError(t("errors.loadFailed"));
        }
        setReport(null);
        return;
      }
      setReport(payload as PropertyFinancialReport);
    } catch {
      setError(t("errors.loadFailed"));
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [checkOutFrom, checkOutTo, propertySlug, t]);

  const exportHref = useCallback(
    (format: "pdf" | "xlsx") => {
      if (!propertySlug) return "#";
      return propertyFinancialReportPath({
        propertySlug,
        checkOutFrom,
        checkOutTo,
        format,
      });
    },
    [checkOutFrom, checkOutTo, propertySlug],
  );

  const canExport = Boolean(report && propertySlug && checkOutFrom && checkOutTo);
  const canSendEmail = Boolean(propertySlug && checkOutFrom && checkOutTo && !sendingEmail);

  const sendEmail = useCallback(async () => {
    if (!propertySlug || !checkOutFrom || !checkOutTo) {
      setError(t("errors.periodInvalid"));
      return;
    }

    const recipients = parseRecipientInput(emailRecipients);
    if (!recipients.length && !selectedProperty?.financial_report_recipients?.trim()) {
      setError(t("emailNoRecipients"));
      return;
    }

    setSendingEmail(true);
    setError("");
    setEmailSuccess("");
    try {
      const outcome = await sendPropertyFinancialReportEmail({
        propertySlug,
        checkOutFrom,
        checkOutTo,
        recipients: recipients.length ? recipients : undefined,
      });
      setEmailSuccess(t("emailSent", { recipients: outcome.recipients.join(", ") }));
    } catch (caught) {
      const parsed =
        caught && typeof caught === "object" && "code" in caught
          ? (caught as PropertyFinancialReportError)
          : parsePropertyFinancialReportError(caught);
      setError(formatEmailError(parsed));
    } finally {
      setSendingEmail(false);
    }
  }, [
    checkOutFrom,
    checkOutTo,
    emailRecipients,
    formatEmailError,
    parseRecipientInput,
    propertySlug,
    selectedProperty?.financial_report_recipients,
    t,
  ]);

  return (
    <div>
      <ReceptionNav tenantName={tenantName} />
      <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
        <ReportsSubNav />
        <div>
          <h1 className="text-xl font-semibold text-stay-navy">{t("title")}</h1>
          <p className="mt-1 text-sm text-muted">{t("subtitle")}</p>
        </div>

        <section className="card space-y-4 p-4">
          <div className="flex flex-wrap items-end gap-3">
            {properties.length > 1 ? (
              <div className="min-w-[220px]">
                <div className="label">{t("property")}</div>
                <select
                  className="input mt-1"
                  value={propertySlug}
                  onChange={(e) => setPropertySlug(e.target.value)}
                >
                  <option value="">{t("propertyPlaceholder")}</option>
                  {properties.map((property) => (
                    <option key={property.slug} value={property.slug}>
                      {property.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : properties.length === 1 ? (
              <div>
                <div className="label">{t("property")}</div>
                <div className="mt-1 text-sm font-medium text-stay-navy">{properties[0].name}</div>
              </div>
            ) : null}

            <div>
              <div className="label">{t("checkOutFrom")}</div>
              <input
                type="date"
                className="input mt-1"
                value={checkOutFrom}
                onChange={(e) => setCheckOutFrom(e.target.value)}
              />
            </div>
            <div>
              <div className="label">{t("checkOutTo")}</div>
              <input
                type="date"
                className="input mt-1"
                value={checkOutTo}
                onChange={(e) => setCheckOutTo(e.target.value)}
              />
            </div>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn-ghost"
                onClick={() => {
                  const period = defaultPreviousMonthPeriod();
                  setCheckOutFrom(period.from);
                  setCheckOutTo(period.to);
                }}
              >
                {t("presetPreviousMonth")}
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => {
                  const period = defaultThisMonthPeriod();
                  setCheckOutFrom(period.from);
                  setCheckOutTo(period.to);
                }}
              >
                {t("presetThisMonth")}
              </button>
            </div>

            <button type="button" className="btn" disabled={loading} onClick={() => void loadReport()}>
              {loading ? tc("loading") : t("generate")}
            </button>
          </div>

          {error ? <p className="text-sm text-red-600">{error}</p> : null}
          {emailSuccess ? <p className="text-sm text-emerald-700">{emailSuccess}</p> : null}

          <div className="min-w-[280px]">
            <div className="label">{t("emailRecipients")}</div>
            <input
              type="text"
              className="input mt-1 w-full"
              value={emailRecipients}
              onChange={(e) => setEmailRecipients(e.target.value)}
              placeholder={selectedProperty?.financial_report_recipients || t("emailRecipientsHint")}
            />
            <p className="mt-1 text-xs text-muted">{t("emailRecipientsHint")}</p>
          </div>

          <div className="flex flex-wrap gap-2 border-t border-stay-border pt-4">
            <a
              href={exportHref("pdf")}
              className={`btn ${canExport ? "" : "pointer-events-none opacity-50"}`}
              aria-disabled={!canExport}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t("downloadPdf")}
            </a>
            <a
              href={exportHref("xlsx")}
              className={`btn ${canExport ? "" : "pointer-events-none opacity-50"}`}
              aria-disabled={!canExport}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t("downloadExcel")}
            </a>
            <button
              type="button"
              className={`btn ${canSendEmail ? "" : "pointer-events-none opacity-50"}`}
              disabled={!canSendEmail}
              onClick={() => void sendEmail()}
            >
              {sendingEmail ? tc("loading") : t("sendEmail")}
            </button>
          </div>
        </section>

        {report ? (
          <section className="card space-y-4 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="font-semibold text-stay-navy">{report.meta.property_name}</h2>
                <p className="text-sm text-muted">
                  {t("periodSummary", {
                    from: report.meta.check_out_from,
                    to: report.meta.check_out_to,
                  })}
                </p>
                <p className="text-xs text-muted">
                  {t("generatedAt", { value: report.meta.generated_at })}
                </p>
              </div>
              <div className="text-sm text-muted">
                {t("maxPeriodDays", { days: report.meta.max_period_days })}
              </div>
            </div>

            {report.meta.rows_with_missing_commission > 0 ? (
              <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {t("missingCommissionWarning", {
                  count: report.meta.rows_with_missing_commission,
                })}
              </p>
            ) : null}

            {report.meta.rows_without_confirmed_payout > 0 ? (
              <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {t("missingPayoutWarning", {
                  count: report.meta.rows_without_confirmed_payout,
                })}
              </p>
            ) : null}

            {report.rows.length === 0 ? (
              <p className="text-muted">{t("noRows")}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[960px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-stay-border text-left">
                      <th className="px-2 py-2 font-medium">{t("columns.booking")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.checkIn")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.checkOut")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.rooms")}</th>
                      <th className="px-2 py-2 font-medium text-right">{t("columns.nights")}</th>
                      <th className="px-2 py-2 font-medium text-right">{t("columns.gross")}</th>
                      <th className="px-2 py-2 font-medium text-right">{t("columns.commission")}</th>
                      <th className="px-2 py-2 font-medium text-right">{t("columns.net")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.payoutStatus")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.payoutDate")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.source")}</th>
                      <th className="px-2 py-2 font-medium">{t("columns.guests")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.rows.map((row) => {
                      const bookingRef = displayBookingReference(row);
                      const externalRef = displayExternalReference(row);
                      return (
                      <tr key={row.reservation_id} className="border-b border-stay-border/70">
                        <td className="px-2 py-2">
                          <div className="font-medium">{bookingRef || tc("dash")}</div>
                          {externalRef ? (
                            <div className="text-xs text-muted">{externalRef}</div>
                          ) : null}
                        </td>
                        <td className="px-2 py-2">{row.check_in}</td>
                        <td className="px-2 py-2">{row.check_out}</td>
                        <td className="px-2 py-2">{row.room_labels.join(", ") || tc("dash")}</td>
                        <td className="px-2 py-2 text-right">{row.nights}</td>
                        <td className="px-2 py-2 text-right">{formatAmount(row.gross)}</td>
                        <td className="px-2 py-2 text-right">{formatAmount(row.commission)}</td>
                        <td className="px-2 py-2 text-right">{formatAmount(row.net)}</td>
                        <td className="px-2 py-2">
                          {t(`payoutStatus.${row.payout_status}`)}
                        </td>
                        <td className="px-2 py-2">
                          {row.payout_received_at || tc("dash")}
                        </td>
                        <td className="px-2 py-2">{row.source || tc("dash")}</td>
                        <td className="px-2 py-2">
                          {row.guests.length
                            ? row.guests.map((guest) => guest.name).join("; ")
                            : tc("dash")}
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-stay-border font-semibold">
                      <td className="px-2 py-3" colSpan={4}>
                        {t("totalsLabel", { count: report.totals.reservation_count })}
                      </td>
                      <td className="px-2 py-3 text-right">{report.totals.nights}</td>
                      <td className="px-2 py-3 text-right">{formatAmount(report.totals.gross)}</td>
                      <td className="px-2 py-3 text-right">{formatAmount(report.totals.commission)}</td>
                      <td className="px-2 py-3 text-right">{formatAmount(report.totals.net)}</td>
                      <td className="px-2 py-3" colSpan={3} />
                    </tr>
                  </tfoot>
                </table>
              </div>
            )}
          </section>
        ) : null}

        <PdvsSection />
      </main>
    </div>
  );
}
