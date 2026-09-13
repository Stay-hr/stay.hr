"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { InvoiceEmailModal } from "@/app/_components/InvoiceEmailModal";
import { invoiceRecipientEmail } from "@/lib/invoiceRecipient";
import {
  reservationInvoicePath,
  reservationInvoicePdfPath,
  reservationInvoiceSendEmailPath,
} from "@/lib/stay-client";
import type { InvoiceSummary, ReservationDetail } from "@/lib/types";

type Props = {
  reservation: ReservationDetail;
  onReservationUpdated: () => void | Promise<void>;
};

function fiscalStatusLabel(
  status: string,
  t: ReturnType<typeof useTranslations<"reservation">>,
): string {
  if (status === "pending") return t("invoiceFiscalPending");
  if (status === "failed") return t("invoiceFiscalFailed");
  if (status === "fiscalized") return t("invoiceFiscalized");
  return status;
}

export function ReservationInvoiceSection({ reservation, onReservationUpdated }: Props) {
  const t = useTranslations("reservation");
  const tc = useTranslations("common");
  const [invoice, setInvoice] = useState<InvoiceSummary | null>(
    reservation.invoice_summary ?? null,
  );
  const [invoiceMissing, setInvoiceMissing] = useState(false);
  const [loading, setLoading] = useState(!reservation.invoice_summary);
  const [sending, setSending] = useState(false);
  const [creating, setCreating] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [emailModalOpen, setEmailModalOpen] = useState(false);

  const loadInvoice = useCallback(async () => {
    setLoading(true);
    setError("");
    setInvoiceMissing(false);
    try {
      const res = await fetch(reservationInvoicePath(reservation.id));
      if (res.status === 404) {
        setInvoice(null);
        setInvoiceMissing(true);
        return;
      }
      if (!res.ok) throw new Error(tc("error"));
      const data = (await res.json()) as InvoiceSummary & { id: number };
      setInvoice({
        id: data.id,
        invoice_number: data.invoice_number,
        fiscal_status: data.fiscal_status,
        jir: data.jir,
        zki: data.zki,
        email_sent_at: data.email_sent_at,
        total: data.total,
        currency: data.currency,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      setLoading(false);
    }
  }, [reservation.id, tc]);

  useEffect(() => {
    if (reservation.invoice_summary) {
      setInvoice(reservation.invoice_summary);
      setInvoiceMissing(false);
      setLoading(false);
      return;
    }
    void loadInvoice();
  }, [reservation.invoice_summary, loadInvoice]);

  const recipientEmail = invoiceRecipientEmail(reservation);
  const primaryInvented = Boolean(
    reservation.guests?.some((guest) => guest.is_primary && guest.evisitor_identity_invented),
  );

  async function postSendEmail(email?: string) {
    setSending(true);
    setMessage("");
    setError("");
    try {
      const res = await fetch(reservationInvoiceSendEmailPath(reservation.id), {
        method: "POST",
        headers: email ? { "Content-Type": "application/json" } : undefined,
        body: email ? JSON.stringify({ email }) : undefined,
      });
      const data = (await res.json().catch(() => null)) as {
        status?: string;
        reason?: string;
        recipient?: string;
      } | null;
      if (!res.ok) {
        if (data?.reason === "no_recipient") {
          setEmailModalOpen(true);
          return;
        }
        if (data?.reason === "no_smtp") {
          throw new Error(t("invoiceSendNoSmtp"));
        }
        if (data?.reason === "no_primary_guest") {
          throw new Error(t("invoiceNoPrimaryGuest"));
        }
        if (data?.reason === "invented_identity") {
          throw new Error(t("invoiceSendInventedIdentity"));
        }
        if (data?.reason === "invalid_email") {
          throw new Error(t("invoiceEmailInvalid"));
        }
        throw new Error(t("invoiceSendFailed"));
      }
      setMessage(t("invoiceEmailSent", { email: data?.recipient || recipientEmail || email || "" }));
      await loadInvoice();
      await onReservationUpdated();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("invoiceSendFailed"));
    } finally {
      setSending(false);
    }
  }

  function handleSendClick() {
    if (recipientEmail) {
      void postSendEmail();
      return;
    }
    setEmailModalOpen(true);
  }

  async function createInvoice() {
    if (reservation.status !== "checked_out") return;
    setCreating(true);
    setMessage("");
    setError("");
    try {
      const res = await fetch(reservationInvoicePath(reservation.id), { method: "POST" });
      const data = (await res.json().catch(() => null)) as {
        invoice_number?: string;
        reason?: string;
        detail?: string;
      } | null;
      if (!res.ok) {
        if (data?.reason === "not_checked_out") {
          throw new Error(t("invoiceCreateNotCheckedOut"));
        }
        if (data?.reason === "fiscal_config_incomplete") {
          throw new Error(t("invoiceCreateFiscalConfig"));
        }
        if (data?.reason === "invoice_build_failed") {
          throw new Error(data.detail || t("invoiceCreateFailed"));
        }
        throw new Error(t("invoiceCreateFailed"));
      }
      setMessage(t("invoiceCreateSuccess", { number: data?.invoice_number || "" }));
      setInvoiceMissing(false);
      await loadInvoice();
      await onReservationUpdated();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("invoiceCreateFailed"));
    } finally {
      setCreating(false);
    }
  }

  if (loading) {
    return (
      <div>
        <h2 className="mb-2 font-semibold">{t("invoiceTitle")}</h2>
        <p className="text-sm text-muted">{tc("loading")}</p>
      </div>
    );
  }

  if (invoiceMissing || !invoice) {
    return (
      <div className="space-y-3">
        <h2 className="mb-2 font-semibold">{t("invoiceTitle")}</h2>
        {error ? <p className="text-sm text-red-600">{error}</p> : null}
        {message ? <p className="text-sm text-emerald-700">{message}</p> : null}
        {reservation.status === "checked_out" ? (
          <button
            type="button"
            className="btn btn-sm"
            disabled={creating}
            onClick={() => void createInvoice()}
          >
            {creating ? tc("loading") : t("createInvoice")}
          </button>
        ) : (
          <p className="text-sm text-muted">{t("invoiceNotFound")}</p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <h2 className="font-semibold">{t("invoiceTitle")}</h2>
      {message ? <p className="text-sm text-emerald-700">{message}</p> : null}
      {error ? <p className="text-sm text-red-600">{error}</p> : null}

      <dl className="grid gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted">{t("invoiceNumber")}</dt>
          <dd className="font-medium">{invoice.invoice_number}</dd>
        </div>
        <div>
          <dt className="text-muted">{t("invoiceFiscalStatus")}</dt>
          <dd className="font-medium">{fiscalStatusLabel(invoice.fiscal_status, t)}</dd>
        </div>
        {invoice.zki ? (
          <div>
            <dt className="text-muted">ZKI</dt>
            <dd className="break-all font-medium">{invoice.zki}</dd>
          </div>
        ) : null}
        {invoice.jir ? (
          <div>
            <dt className="text-muted">JIR</dt>
            <dd className="break-all font-medium">{invoice.jir}</dd>
          </div>
        ) : null}
        {invoice.total ? (
          <div>
            <dt className="text-muted">{t("invoiceTotal")}</dt>
            <dd className="font-medium">
              {invoice.total} {invoice.currency || reservation.currency}
            </dd>
          </div>
        ) : null}
      </dl>

      {invoice.email_sent_at ? (
        <p className="text-sm">
          <span className="badge bg-emerald-100 text-emerald-800">{t("invoiceEmailSentBadge")}</span>
          <span className="ml-2 text-muted">
            {new Date(invoice.email_sent_at).toLocaleString()}
          </span>
        </p>
      ) : null}

      {primaryInvented ? (
        <p className="text-sm text-amber-800">{t("invoiceSendInventedIdentity")}</p>
      ) : !recipientEmail ? (
        <p className="text-sm text-amber-800">{t("invoiceNoEmailHint")}</p>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <a
          href={reservationInvoicePdfPath(reservation.id)}
          target="_blank"
          rel="noopener noreferrer"
          className="btn btn-sm"
        >
          {t("downloadInvoicePdf")}
        </a>
        {primaryInvented ? null : (
          <button
            type="button"
            className="btn btn-sm"
            disabled={sending}
            onClick={handleSendClick}
          >
            {sending ? tc("loading") : t("sendInvoiceEmail")}
          </button>
        )}
      </div>

      <InvoiceEmailModal
        open={emailModalOpen}
        onClose={() => setEmailModalOpen(false)}
        onConfirm={async (email) => {
          await postSendEmail(email);
        }}
      />
    </div>
  );
}
