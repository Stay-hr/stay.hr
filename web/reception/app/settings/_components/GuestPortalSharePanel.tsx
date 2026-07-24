"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  guestSettingsSharePath,
  type ShareChannel,
  type SharePortalResponse,
} from "@/lib/guestSettings";

type Props = {
  propertyId: number;
  enabled: boolean;
};

export function GuestPortalSharePanel({ propertyId, enabled }: Props) {
  const t = useTranslations("settings.guest.share");
  const [reservationId, setReservationId] = useState("");
  const [channel, setChannel] = useState<"auto" | ShareChannel>("auto");
  const [sharing, setSharing] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<SharePortalResponse | null>(null);

  if (!enabled) {
    return <p className="text-sm text-stay-muted">{t("unavailable")}</p>;
  }

  async function onShare() {
    const id = Number.parseInt(reservationId.trim(), 10);
    if (!Number.isFinite(id) || id <= 0) {
      setError(t("reservationRequired"));
      return;
    }
    setSharing(true);
    setError("");
    setResult(null);
    try {
      const body: Record<string, unknown> = {
        kind: "portal",
        target: "reservation",
        reservation_id: id,
      };
      if (channel !== "auto") {
        body.channel = channel;
      }
      const res = await fetch(guestSettingsSharePath(propertyId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json().catch(() => null);
      if (!res.ok) {
        const detail =
          typeof json?.detail === "string" ? json.detail : t("failed");
        setError(detail);
        return;
      }
      setResult(json as SharePortalResponse);
    } catch {
      setError(t("failed"));
    } finally {
      setSharing(false);
    }
  }

  return (
    <div className="space-y-3 rounded border border-stay-border bg-white p-4">
      <h3 className="text-sm font-semibold text-stay-navy">{t("title")}</h3>
      <p className="text-sm text-stay-muted">{t("hint")}</p>
      <label className="block space-y-1">
        <span className="text-sm font-medium text-stay-navy">{t("reservationId")}</span>
        <input
          className="w-full rounded border border-stay-border bg-white px-3 py-2 text-sm text-stay-navy"
          inputMode="numeric"
          value={reservationId}
          onChange={(e) => setReservationId(e.target.value)}
          placeholder={t("reservationPlaceholder")}
        />
      </label>
      <label className="block space-y-1">
        <span className="text-sm font-medium text-stay-navy">{t("channel")}</span>
        <select
          className="w-full rounded border border-stay-border bg-white px-3 py-2 text-sm text-stay-navy"
          value={channel}
          onChange={(e) => setChannel(e.target.value as "auto" | ShareChannel)}
        >
          <option value="auto">{t("channelAuto")}</option>
          <option value="booking">{t("channelBooking")}</option>
          <option value="whatsapp">{t("channelWhatsapp")}</option>
          <option value="email">{t("channelEmail")}</option>
        </select>
      </label>
      <button
        type="button"
        onClick={() => void onShare()}
        disabled={sharing}
        className="rounded bg-stay-navy px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
      >
        {sharing ? t("sharing") : t("submit")}
      </button>
      {error ? <p className="text-sm text-red-600">{error}</p> : null}
      {result ? (
        <div className="space-y-1 text-sm text-stay-navy">
          <p className="text-green-700">
            {t("success", { status: result.status, channel: result.channel })}
          </p>
          {result.portal_url ? (
            <p className="break-all text-stay-muted">
              {t("portalUrl")}: {result.portal_url}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
