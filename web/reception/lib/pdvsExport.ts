/**
 * PDV-S (EU reverse-charge) reception helpers.
 *
 * Period is ForeignServiceInvoice.tax_period — independent of the property
 * financial report checkout filters on this page.
 */

export type PdvsReadiness = {
  configured: boolean;
  missing: string[];
  warnings: string[];
};

export type PdvsInvoice = {
  id: number;
  provider: string;
  invoice_number: string;
  tax_period: string;
  taxable_amount: string;
  currency: string;
  supplier_name: string;
  supplier_country: string;
  supplier_vat_id: string;
  invoice_date: string;
  imported_at: string;
  created?: boolean;
  already_imported?: boolean;
};

export type PdvsInvoiceList = PdvsReadiness & {
  period: string;
  count: number;
  results: PdvsInvoice[];
};

export function pdvsStatusPath(): string {
  return "/api/stay/reception/eporezna/status/";
}

export function pdvsInvoicesPath(period: string): string {
  const q = new URLSearchParams({ period });
  return `/api/stay/reception/eporezna/foreign-service-invoices/?${q}`;
}

export function pdvsInvoicesUploadPath(): string {
  return "/api/stay/reception/eporezna/foreign-service-invoices/";
}

export function pdvsExportPath(period: string): string {
  const q = new URLSearchParams({ period });
  return `/api/stay/reception/eporezna/pdvs/?${q}`;
}

export function pdvExportPath(period: string): string {
  const q = new URLSearchParams({ period });
  return `/api/stay/reception/eporezna/pdv/?${q}`;
}

export async function fetchPdvsStatus(): Promise<PdvsReadiness> {
  const res = await fetch(pdvsStatusPath());
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(
      (typeof data?.detail === "string" && data.detail) || "status_failed",
    );
  }
  return data as PdvsReadiness;
}

export async function fetchPdvsInvoices(period: string): Promise<PdvsInvoiceList> {
  const res = await fetch(pdvsInvoicesPath(period));
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(
      (typeof data?.detail === "string" && data.detail) || "list_failed",
    );
  }
  return data as PdvsInvoiceList;
}

export async function uploadPdvsInvoice(file: File): Promise<PdvsInvoice> {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(pdvsInvoicesUploadPath(), {
    method: "POST",
    body: formData,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(
      (typeof data?.detail === "string" && data.detail) || "upload_failed",
    );
  }
  return data as PdvsInvoice;
}

async function downloadXmlAttachment(
  url: string,
  fallbackFilename: string,
): Promise<void> {
  const res = await fetch(url);
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new Error(
      (typeof data?.detail === "string" && data.detail) || "export_failed",
    );
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = /filename="([^"]+)"/i.exec(disposition);
  const filename = match?.[1] || fallbackFilename;
  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

/** Download XML using backend Content-Disposition filename (do not invent names). */
export async function downloadPdvsXml(period: string): Promise<void> {
  await downloadXmlAttachment(pdvsExportPath(period), `PDV-S_${period}.xml`);
}

export async function downloadPdvXml(period: string): Promise<void> {
  await downloadXmlAttachment(pdvExportPath(period), `PDV_${period}.xml`);
}

export function upsertInvoiceById(
  list: PdvsInvoice[],
  invoice: PdvsInvoice,
): PdvsInvoice[] {
  const idx = list.findIndex((row) => row.id === invoice.id);
  if (idx === -1) {
    return [invoice, ...list];
  }
  const next = list.slice();
  next[idx] = invoice;
  return next;
}

export function defaultPdvsPeriod(): string {
  const now = new Date();
  const prev = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  const y = prev.getUTCFullYear();
  const m = String(prev.getUTCMonth() + 1).padStart(2, "0");
  return `${y}-${m}`;
}
