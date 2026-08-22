"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { reservationPaymentInstructionsSendPath } from "@/lib/stay-client";
import type { ReservationDetail } from "@/lib/types";

type Channel = "whatsapp" | "email";

type Props = {
  reservation: ReservationDetail;
  onSent?: () => void;
};

export function ReservationPaymentSendSection({ reservation, onSent }: Props) {
  const t = useTranslations("reservation.paymentSend");
  const [channel, setChannel] = useState<Channel>("whatsapp");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ status: string; payment_url?: string } | null>(null);

  const canSend =
    reservation.status !== "canceled" &&
    reservation.status !== "refused" &&
    reservation.status !== "no_show" &&
    Boolean(reservation.total_amount);

  if (!canSend) {
    return null;
  }

  async function onSend() {
    setSending(true);
    setError("");
    setResult(null);
    try {
      const res = await fetch(reservationPaymentInstructionsSendPath(reservation.id), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel }),
      });
      const json = (await res.json().catch(() => null)) as {
        detail?: string;
        status?: string;
        payment_url?: string;
      } | null;
      if (!res.ok) {
        setError(typeof json?.detail === "string" ? json.detail : t("failed"));
        return;
      }
      setResult({ status: json?.status || "sent", payment_url: json?.payment_url });
      onSent?.();
    } catch {
      setError(t("failed"));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="rounded border border-border bg-white p-4">
      <h2 className="mb-2 font-semibold">{t("title")}</h2>
      <p className="mb-3 text-sm text-muted">{t("hint")}</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block space-y-1">
          <span className="text-sm font-medium">{t("channel")}</span>
          <select
            className="rounded border border-border bg-white px-3 py-2 text-sm"
            value={channel}
            onChange={(e) => setChannel(e.target.value as Channel)}
          >
            <option value="whatsapp">{t("channelWhatsapp")}</option>
            <option value="email">{t("channelEmail")}</option>
          </select>
        </label>
        <button
          type="button"
          className="btn btn-sm"
          disabled={sending}
          onClick={() => void onSend()}
        >
          {sending ? t("sending") : t("submit")}
        </button>
      </div>
      {error ? <p className="mt-2 text-sm text-red-600">{error}</p> : null}
      {result ? (
        <div className="mt-2 space-y-1 text-sm text-emerald-700">
          <p>{t("success", { status: result.status })}</p>
          {result.payment_url ? (
            <p className="break-all text-muted">
              {t("paymentUrl")}: {result.payment_url}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
