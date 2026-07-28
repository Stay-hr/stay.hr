"use client";

/**
 * PDV-S period is ForeignServiceInvoice.tax_period (foreign invoice tax month).
 * It is intentionally independent of the property-financial checkout date filters.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  defaultPdvsPeriod,
  downloadPdvsXml,
  fetchPdvsInvoices,
  uploadPdvsInvoice,
  upsertInvoiceById,
  type PdvsInvoice,
} from "@/lib/pdvsExport";

function formatPeriodLabel(period: string, locale: string): string {
  const [y, m] = period.split("-").map(Number);
  if (!y || !m) return period;
  const date = new Date(Date.UTC(y, m - 1, 1));
  return new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function formatImportedAt(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(d);
}

export function PdvsSection() {
  const t = useTranslations("propertyFinancialReport.pdvs");
  const locale = useLocale();
  const fileRef = useRef<HTMLInputElement>(null);

  const [period, setPeriod] = useState(defaultPdvsPeriod);
  const [configured, setConfigured] = useState(true);
  const [missing, setMissing] = useState<string[]>([]);
  const [invoices, setInvoices] = useState<PdvsInvoice[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async (nextPeriod: string) => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchPdvsInvoices(nextPeriod);
      setConfigured(data.configured);
      setMissing(data.missing ?? []);
      setInvoices(data.results ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadError"));
      setInvoices([]);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load(period);
  }, [period, load]);

  async function onUpload(file: File | null) {
    if (!file) return;
    setUploading(true);
    setError("");
    setMessage("");
    try {
      const invoice = await uploadPdvsInvoice(file);
      if (invoice.tax_period !== period) {
        setPeriod(invoice.tax_period);
      }
      setInvoices((prev) => upsertInvoiceById(prev, invoice));
      if (invoice.already_imported || invoice.created === false) {
        setMessage(
          t("alreadyImported", {
            at: formatImportedAt(invoice.imported_at, locale),
          }),
        );
      } else {
        setMessage(t("uploadSuccess", { number: invoice.invoice_number }));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("uploadError"));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function onExport() {
    setExporting(true);
    setError("");
    setMessage("");
    try {
      await downloadPdvsXml(period);
      setMessage(t("exportSuccess"));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("exportError"));
    } finally {
      setExporting(false);
    }
  }

  const periodLabel = formatPeriodLabel(period, locale);
  const actionsDisabled = !configured || loading;

  return (
    <section className="mt-10 rounded-xl border border-stay-border bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold text-stay-ink">{t("title")}</h2>
        <p className="text-sm text-muted">
          {t("badge", { period: periodLabel, count: invoices.length })}
        </p>
      </div>
      <p className="mt-1 text-sm text-muted">{t("subtitle")}</p>

      <div className="mt-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">{t("period")}</span>
          <input
            type="month"
            className="rounded-md border border-stay-border px-3 py-2"
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(e) => void onUpload(e.target.files?.[0] ?? null)}
          />
          <button
            type="button"
            className="rounded-md bg-stay-ink px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={actionsDisabled || uploading}
            onClick={() => fileRef.current?.click()}
          >
            {uploading ? t("uploading") : t("upload")}
          </button>
          <button
            type="button"
            className="rounded-md border border-stay-border px-3 py-2 text-sm font-medium disabled:opacity-50"
            disabled={actionsDisabled || exporting || invoices.length === 0}
            onClick={() => void onExport()}
          >
            {exporting ? t("exporting") : t("export")}
          </button>
        </div>
      </div>

      {!configured ? (
        <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950">
          <p className="font-medium">{t("notConfigured")}</p>
          {missing.length ? (
            <p className="mt-1">{t("missing", { fields: missing.join(", ") })}</p>
          ) : null}
        </div>
      ) : null}

      {message ? (
        <p className="mt-3 text-sm text-stay-ink" role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="mt-3 text-sm text-red-700" role="alert">
          {error}
        </p>
      ) : null}

      <div className="mt-5">
        <h3 className="text-sm font-semibold">
          {t("listTitle", { period: periodLabel })}
        </h3>
        {loading ? (
          <p className="mt-2 text-sm text-muted">{t("loading")}</p>
        ) : invoices.length === 0 ? (
          <p className="mt-2 text-sm text-muted">{t("empty")}</p>
        ) : (
          <ul className="mt-2 divide-y divide-stay-border/70 rounded-md border border-stay-border">
            {invoices.map((inv) => (
              <li key={inv.id} className="flex flex-wrap items-baseline justify-between gap-2 px-3 py-2 text-sm">
                <div>
                  <div className="font-medium">
                    {inv.supplier_name} · {inv.invoice_number}
                  </div>
                  <div className="text-xs text-muted">
                    {inv.supplier_country}
                    {inv.supplier_vat_id} · {t("importedAt", {
                      at: formatImportedAt(inv.imported_at, locale),
                    })}
                  </div>
                </div>
                <div className="font-medium tabular-nums">
                  {inv.taxable_amount} {inv.currency}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
