"use client";

/**
 * PDV-S period is ForeignServiceInvoice.tax_period (foreign invoice tax month).
 * It is intentionally independent of the property-financial checkout date filters.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  defaultPdvsPeriod,
  downloadPdvXml,
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

type PdvsCopy = (key: string, values?: Record<string, string>) => string;

/** Pure outcome copy for upload + dual XML export (testable). */
export function buildUploadExportFeedback(args: {
  t: PdvsCopy;
  duplicate: boolean;
  invoiceNumber: string;
  importedAtLabel: string;
  pdvsOk: boolean;
  pdvOk: boolean;
}): { message: string; error: string } {
  const { t, duplicate, invoiceNumber, importedAtLabel, pdvsOk, pdvOk } = args;
  const importNote = duplicate
    ? t("alreadyImported", { at: importedAtLabel })
    : t("uploadSuccess", { number: invoiceNumber });

  if (pdvsOk && pdvOk) {
    return {
      message: `${importNote} ${t("exportBothSuccess")}`,
      error: "",
    };
  }
  if (pdvsOk && !pdvOk) {
    return {
      message: `${importNote} ${t("exportPartialPdvsOnly")}`,
      error: t("exportPdvError"),
    };
  }
  if (!pdvsOk && pdvOk) {
    return {
      message: `${importNote} ${t("exportPartialPdvOnly")}`,
      error: t("exportError"),
    };
  }
  return {
    message: importNote,
    error: t("exportBothFailed"),
  };
}

export function PdvsSection() {
  const t = useTranslations("propertyFinancialReport.pdvs");
  const locale = useLocale();
  const fileRef = useRef<HTMLInputElement>(null);
  const skipNextPeriodLoadRef = useRef(false);

  const [period, setPeriod] = useState(defaultPdvsPeriod);
  const [configured, setConfigured] = useState(true);
  const [missing, setMissing] = useState<string[]>([]);
  const [invoices, setInvoices] = useState<PdvsInvoice[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportingPdv, setExportingPdv] = useState(false);
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
    if (skipNextPeriodLoadRef.current) {
      skipNextPeriodLoadRef.current = false;
      return;
    }
    void load(period);
  }, [period, load]);

  async function onUpload(file: File | null) {
    if (!file || busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const invoice = await uploadPdvsInvoice(file);
      const uploadedPeriod = invoice.tax_period;
      const duplicate =
        Boolean(invoice.already_imported) || invoice.created === false;

      // Immediate UX; load(uploadedPeriod) below is authoritative.
      setInvoices((prev) => upsertInvoiceById(prev, invoice));
      skipNextPeriodLoadRef.current = true;
      setPeriod(uploadedPeriod);
      await load(uploadedPeriod);

      const [pdvsResult, pdvResult] = await Promise.allSettled([
        downloadPdvsXml(uploadedPeriod),
        downloadPdvXml(uploadedPeriod),
      ]);

      const feedback = buildUploadExportFeedback({
        t: t as PdvsCopy,
        duplicate,
        invoiceNumber: invoice.invoice_number,
        importedAtLabel: formatImportedAt(invoice.imported_at, locale),
        pdvsOk: pdvsResult.status === "fulfilled",
        pdvOk: pdvResult.status === "fulfilled",
      });
      setMessage(feedback.message);
      setError(feedback.error);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("uploadError"));
      setMessage("");
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function onExport() {
    if (busy) return;
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

  async function onExportPdv() {
    if (busy) return;
    setExportingPdv(true);
    setError("");
    setMessage("");
    try {
      await downloadPdvXml(period);
      setMessage(t("exportPdvSuccess"));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("exportPdvError"));
    } finally {
      setExportingPdv(false);
    }
  }

  const periodLabel = formatPeriodLabel(period, locale);
  const actionsDisabled = !configured || loading || busy;

  return (
    <section className="mt-10 rounded-xl border border-stay-border bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold text-stay-navy">{t("title")}</h2>
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
            disabled={busy}
            onChange={(e) => setPeriod(e.target.value)}
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            data-testid="pdvs-file-input"
            onChange={(e) => void onUpload(e.target.files?.[0] ?? null)}
          />
          <button
            type="button"
            className="btn"
            disabled={actionsDisabled}
            onClick={() => fileRef.current?.click()}
          >
            {busy ? t("uploading") : t("upload")}
          </button>
          <button
            type="button"
            className="btn-ghost"
            disabled={actionsDisabled || exporting || invoices.length === 0}
            onClick={() => void onExport()}
          >
            {exporting ? t("exporting") : t("export")}
          </button>
          <button
            type="button"
            className="btn-ghost"
            disabled={actionsDisabled || exportingPdv || invoices.length === 0}
            onClick={() => void onExportPdv()}
          >
            {exportingPdv ? t("exportingPdv") : t("exportPdv")}
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
        <p className="mt-3 text-sm text-stay-navy" role="status">
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
