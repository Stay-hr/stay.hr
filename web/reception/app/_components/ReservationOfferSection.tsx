"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  publicOfferPdfPath,
  reservationOfferPath,
  reservationOfferPdfPath,
  reservationOfferSendEmailPath,
} from "@/lib/stay-client";
import type { OfferSummary, ReservationDetail } from "@/lib/types";

type Props = {
  reservation: ReservationDetail;
  onUpdated?: () => void;
};

export function ReservationOfferSection({ reservation, onUpdated }: Props) {
  const t = useTranslations("reservation.offer");
  const [offer, setOffer] = useState<OfferSummary | null>(reservation.offer_summary ?? null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const canOffer =
    reservation.status !== "canceled" &&
    reservation.status !== "refused" &&
    reservation.status !== "no_show" &&
    Boolean(reservation.total_amount);

  const loadOffer = useCallback(async () => {
    if (!canOffer) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(reservationOfferPath(reservation.id));
      if (res.status === 404) {
        setOffer(null);
        return;
      }
      if (!res.ok) throw new Error(t("loadFailed"));
      setOffer((await res.json()) as OfferSummary);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [canOffer, reservation.id, t]);

  useEffect(() => {
    if (reservation.offer_summary) {
      setOffer(reservation.offer_summary);
      return;
    }
    void loadOffer();
  }, [reservation.offer_summary, loadOffer]);

  if (!canOffer) {
    return null;
  }

  async function onGenerate() {
    setGenerating(true);
    setError("");
    setMessage("");
    try {
      const res = await fetch(reservationOfferPath(reservation.id), { method: "POST" });
      const data = (await res.json().catch(() => null)) as OfferSummary & {
        detail?: string;
        reason?: string;
      };
      if (!res.ok) {
        if (data?.reason === "no_amount") {
          setError(t("noAmount"));
          return;
        }
        if (data?.reason === "fiscal_config_incomplete") {
          setError(t("fiscalConfig"));
          return;
        }
        setError(typeof data?.detail === "string" ? data.detail : t("generateFailed"));
        return;
      }
      setOffer(data);
      setMessage(t("generateSuccess", { number: data.offer_number }));
      onUpdated?.();
    } catch {
      setError(t("generateFailed"));
    } finally {
      setGenerating(false);
    }
  }

  async function onSendEmail() {
    setSending(true);
    setError("");
    setMessage("");
    try {
      const res = await fetch(reservationOfferSendEmailPath(reservation.id), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = (await res.json().catch(() => null)) as {
        detail?: string;
        reason?: string;
        recipient?: string;
        status?: string;
      };
      if (!res.ok) {
        if (data?.reason === "no_smtp") {
          setError(t("noSmtp"));
          return;
        }
        if (data?.reason === "no_recipient") {
          setError(t("noRecipient"));
          return;
        }
        setError(typeof data?.detail === "string" ? data.detail : t("sendFailed"));
        return;
      }
      setMessage(t("emailSent", { email: data?.recipient || reservation.invoice_email || "" }));
      await loadOffer();
      onUpdated?.();
    } catch {
      setError(t("sendFailed"));
    } finally {
      setSending(false);
    }
  }

  const publicPdfUrl =
    offer?.public_access_token != null
      ? publicOfferPdfPath(offer.public_access_token)
      : null;

  return (
    <div className="rounded border border-border bg-white p-4">
      <h2 className="mb-2 font-semibold">{t("title")}</h2>
      <p className="mb-3 text-sm text-muted">{t("hint")}</p>

      {loading ? <p className="text-sm text-muted">{t("loading")}</p> : null}

      {offer ? (
        <dl className="mb-3 grid gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted">{t("number")}</dt>
            <dd className="font-medium">{offer.offer_number}</dd>
          </div>
          {offer.buyer_name ? (
            <div>
              <dt className="text-muted">{t("buyer")}</dt>
              <dd className="font-medium">{offer.buyer_name}</dd>
            </div>
          ) : null}
          {offer.total ? (
            <div>
              <dt className="text-muted">{t("total")}</dt>
              <dd className="font-medium">
                {offer.total} {offer.currency || reservation.currency || "EUR"}
              </dd>
            </div>
          ) : null}
          {offer.payment_reference ? (
            <div>
              <dt className="text-muted">{t("paymentReference")}</dt>
              <dd className="font-medium">{offer.payment_reference}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {!offer ? (
          <button
            type="button"
            className="btn btn-sm"
            disabled={generating}
            onClick={() => void onGenerate()}
          >
            {generating ? t("generating") : t("generate")}
          </button>
        ) : (
          <>
            <a
              href={reservationOfferPdfPath(reservation.id)}
              className="btn btn-sm"
              target="_blank"
              rel="noopener noreferrer"
            >
              {t("downloadPdf")}
            </a>
            {publicPdfUrl ? (
              <a
                href={publicPdfUrl}
                className="btn btn-sm"
                target="_blank"
                rel="noopener noreferrer"
              >
                {t("publicPdf")}
              </a>
            ) : null}
            <button
              type="button"
              className="btn btn-sm"
              disabled={sending}
              onClick={() => void onSendEmail()}
            >
              {sending ? t("sending") : t("sendEmail")}
            </button>
          </>
        )}
      </div>

      {offer?.email_sent_at ? (
        <p className="mt-2 text-sm text-emerald-700">{t("emailSentBadge")}</p>
      ) : null}
      {message ? <p className="mt-2 text-sm text-emerald-700">{message}</p> : null}
      {error ? <p className="mt-2 text-sm text-red-600">{error}</p> : null}
    </div>
  );
}
