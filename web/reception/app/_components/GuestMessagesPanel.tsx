"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  ensureCorrelationId,
  logGuestMessageEvent,
  sanitizeBody,
  syncCorrelationIdFromResponse,
} from "@/lib/guestMessageDebug";
import type {
  GuestMessageChannels,
  GuestMessageComposeResponse,
  GuestMessageTimelineItem,
} from "@/lib/types";
import { useReservationVersionWatch } from "@/lib/useReservationVersionWatch";

type Props = {
  reservationId: number;
};

type ComposeIntent = "checkin" | "reply" | "custom";

const CHANNEL_ORDER = ["email", "whatsapp", "booking"] as const;

function formatMessageTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function channelLabelKey(channel: string): string {
  if (channel === "booking") return "channelBooking";
  if (channel === "whatsapp") return "channelWhatsapp";
  return "channelEmail";
}

function timelineChannelLabels(
  item: GuestMessageTimelineItem,
  t: (key: string) => string,
): string {
  const channels = item.channels?.length ? item.channels : [item.channel];
  return channels.map((channel) => t(channelLabelKey(channel))).join(" · ");
}

function channelHint(
  channel: string,
  channels: GuestMessageChannels,
  t: (key: string, values?: Record<string, string>) => string,
  composeIntent?: ComposeIntent,
): string | null {
  if (channel === "booking" && channels.booking?.available) {
    return t("channelBookingHint");
  }
  if (channel === "email" && channels.email?.available && channels.email.to) {
    return t("channelEmailHint", { email: channels.email.to });
  }
  if (channel === "whatsapp" && channels.whatsapp?.available) {
    const wa = channels.whatsapp;
    if (wa.api_send && !wa.session_open) {
      const templateOk =
        composeIntent === "checkin" && Boolean(wa.template_available);
      if (!templateOk) {
        return t("channelWhatsappSessionClosedHint");
      }
      return t("channelWhatsappApiHint");
    }
    if (wa.api_send) {
      return t("channelWhatsappApiHint");
    }
    const phone = wa.phone_raw || wa.phone_wa || "";
    if (phone) {
      return t("channelWhatsappHint", { phone });
    }
  }
  return null;
}

export function GuestMessagesPanel({ reservationId }: Props) {
  const t = useTranslations("guestMessages");
  const tc = useTranslations("common");
  const [timeline, setTimeline] = useState<GuestMessageTimelineItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [composeIntent, setComposeIntent] = useState<ComposeIntent>("checkin");
  const [composeHint, setComposeHint] = useState("");
  const [draftId, setDraftId] = useState<number | null>(null);
  const [bodyText, setBodyText] = useState("");
  const [channels, setChannels] = useState<GuestMessageChannels>({});
  const [selectedChannel, setSelectedChannel] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [actionMessage, setActionMessage] = useState("");
  const [correlationId, setCorrelationId] = useState("");
  const channelLoggedRef = useRef("");

  const baseUrl = `/api/stay/reception/reservations/${reservationId}/messages`;

  const availableChannels = useMemo(
    () => CHANNEL_ORDER.filter((key) => channels[key]?.available),
    [channels],
  );

  const loadTimeline = useCallback(
    async (opts?: { background?: boolean }) => {
      const background = Boolean(opts?.background);
      if (!background) {
        setLoading(true);
      }
      setError("");
      try {
        // ADR 0019 Phase A: conversation GET is DB-only (sync=0).
        const res = await fetch(`${baseUrl}/?sync=0`);
        if (!res.ok) throw new Error(t("loadFailed"));
        setTimeline((await res.json()) as GuestMessageTimelineItem[]);
      } catch (err) {
        setError(err instanceof Error ? err.message : tc("error"));
        if (!background) {
          setTimeline([]);
        }
      } finally {
        if (!background) {
          setLoading(false);
        }
      }
    },
    [baseUrl, t, tc],
  );

  useEffect(() => {
    void loadTimeline({ background: false });
  }, [reservationId, loadTimeline]);

  useReservationVersionWatch({
    reservationId,
    scope: "messages",
    transport: "poll",
    onVersionChange: () => {
      void loadTimeline({ background: true });
    },
  });

  useEffect(() => {
    if (availableChannels.length === 0) {
      setSelectedChannel("");
      return;
    }
    if (
      selectedChannel &&
      availableChannels.includes(selectedChannel as (typeof CHANNEL_ORDER)[number])
    ) {
      return;
    }
    const preferred = channels.default_channel;
    if (
      preferred &&
      availableChannels.includes(preferred as (typeof CHANNEL_ORDER)[number])
    ) {
      setSelectedChannel(preferred);
      return;
    }
    setSelectedChannel(availableChannels[0]);
  }, [availableChannels, channels.default_channel, selectedChannel]);

  useEffect(() => {
    if (!correlationId || !selectedChannel || draftId === null) return;
    if (channelLoggedRef.current === `${correlationId}:${selectedChannel}`) return;
    channelLoggedRef.current = `${correlationId}:${selectedChannel}`;
    logGuestMessageEvent("channel.change", {
      correlationId,
      selectedChannel,
      defaultChannel: channels.default_channel ?? "",
      channelHint: channelHint(selectedChannel, channels, t, composeIntent),
    });
  }, [correlationId, selectedChannel, channels, draftId, t, composeIntent]);

  async function handleCompose() {
    const cid = ensureCorrelationId(null);
    setCorrelationId(cid);
    channelLoggedRef.current = "";
    const composeStarted = performance.now();
    setBusy(true);
    setError("");
    setActionMessage("");
    logGuestMessageEvent("compose.start", {
      correlationId: cid,
      reservationId,
      composeIntent,
    });
    try {
      const payload: Record<string, string> = { intent: composeIntent };
      if (composeIntent === "reply" && composeHint.trim()) {
        payload.hint = composeHint.trim();
      }
      if (composeIntent === "custom" && composeHint.trim()) {
        payload.hint = composeHint.trim();
      }
      const res = await fetch(`${baseUrl}/compose/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Correlation-Id": cid,
        },
        body: JSON.stringify(payload),
      });
      const echoedId = syncCorrelationIdFromResponse(res, cid);
      setCorrelationId(echoedId);
      const composeMs = Math.round(performance.now() - composeStarted);
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { detail?: string } | null;
        logGuestMessageEvent("compose.error", {
          correlationId: echoedId,
          reservationId,
          composeIntent,
          compose_ms: composeMs,
          httpStatus: res.status,
          error: data?.detail || t("composeFailed"),
        });
        throw new Error(data?.detail || t("composeFailed"));
      }
      const data = (await res.json()) as GuestMessageComposeResponse;
      logGuestMessageEvent("compose.success", {
        correlationId: echoedId,
        reservationId,
        composeIntent,
        compose_ms: composeMs,
        draftId: data.draft_id,
        channels: data.channels,
      });
      setDraftId(data.draft_id);
      setBodyText(data.body_text);
      setChannels(data.channels);
      setActionMessage(t("composeReady"));
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      setBusy(false);
    }
  }

  async function handleSend() {
    if (draftId === null) {
      setError(t("composeFirst"));
      return;
    }
    const text = bodyText.trim();
    if (!text) {
      setError(t("emptyBody"));
      return;
    }
    if (!selectedChannel) {
      setError(t("noChannel"));
      return;
    }

    setBusy(true);
    setError("");
    setActionMessage("");
    const cid = ensureCorrelationId(correlationId);
    const sendStarted = performance.now();
    logGuestMessageEvent("send.start", {
      correlationId: cid,
      reservationId,
      draftId,
      selectedChannel,
      ...sanitizeBody(text),
    });
    try {
      const res = await fetch(`${baseUrl}/send/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Correlation-Id": cid,
        },
        body: JSON.stringify({
          draft_id: draftId,
          channel: selectedChannel,
          body_text: text,
        }),
      });
      const echoedId = syncCorrelationIdFromResponse(res, cid);
      setCorrelationId(echoedId);
      const sendMs = Math.round(performance.now() - sendStarted);
      const data = (await res.json().catch(() => null)) as
        | (GuestMessageTimelineItem & {
            wa_me_url?: string | null;
            handoff_reason?: string | null;
            provider_message_id?: string | null;
          })
        | { detail?: string; channel?: string[] }
        | null;
      if (!res.ok) {
        const detail =
          (data && "channel" in data && Array.isArray(data.channel) && data.channel[0]) ||
          (data && "detail" in data && data.detail) ||
          t("sendFailed");
        const errorText =
          String(detail) === "whatsapp_template_required"
            ? t("sendErrorWhatsappTemplateRequired")
            : String(detail);
        logGuestMessageEvent("send.error", {
          correlationId: echoedId,
          reservationId,
          draftId,
          selectedChannel,
          send_ms: sendMs,
          httpStatus: res.status,
          error: errorText,
          ...sanitizeBody(text),
        });
        throw new Error(errorText);
      }

      let popupBlocked = false;
      if (
        selectedChannel === "whatsapp" &&
        data &&
        "status" in data &&
        data.status === "handoff_whatsapp" &&
        "wa_me_url" in data &&
        data.wa_me_url
      ) {
        const popup = window.open(data.wa_me_url, "_blank", "noopener,noreferrer");
        popupBlocked = popup === null;
        if (popupBlocked) {
          logGuestMessageEvent("send.popup_blocked", {
            correlationId: echoedId,
            reservationId,
            draftId,
            selectedChannel,
            wa_me_url: data.wa_me_url,
          });
        }
        setActionMessage(t("whatsappHandoff"));
      } else {
        setActionMessage(t("sendSuccess"));
      }

      logGuestMessageEvent("send.success", {
        correlationId: echoedId,
        reservationId,
        draftId,
        selectedChannel,
        send_ms: sendMs,
        status: data && "status" in data ? data.status : null,
        handoff_reason:
          data && "handoff_reason" in data ? data.handoff_reason ?? null : null,
        wa_me_url: data && "wa_me_url" in data ? data.wa_me_url ?? null : null,
        provider_message_id:
          data && "provider_message_id" in data
            ? data.provider_message_id ?? null
            : null,
        popupBlocked,
        ...sanitizeBody(text),
      });

      setDraftId(null);
      setBodyText("");
      setComposeHint("");
      setCorrelationId("");
      channelLoggedRef.current = "";
      await loadTimeline({ background: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      setBusy(false);
    }
  }

  function handleManualRefresh() {
    void loadTimeline({ background: false });
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-semibold">{t("title")}</h2>
        <button
          type="button"
          className="btn-ghost text-sm"
          onClick={handleManualRefresh}
          disabled={loading || busy}
        >
          {tc("refresh")}
        </button>
      </div>

      <div className="max-h-72 space-y-2 overflow-y-auto rounded-lg border bg-stay-surface/40 p-3">
        {loading ? (
          <p className="text-sm text-muted">{tc("loading")}</p>
        ) : timeline.length === 0 ? (
          <p className="text-sm text-muted">{t("empty")}</p>
        ) : (
          timeline.map((item) => {
            const outbound = item.direction === "outbound";
            return (
              <div
                key={`${item.source}-${item.id}`}
                className={`flex ${outbound ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${
                    outbound
                      ? "rounded-br-md bg-stay-blue text-white"
                      : "rounded-bl-md border bg-white text-stay-navy"
                  }`}
                >
                  <div className="mb-1 flex flex-wrap items-center gap-2 text-xs opacity-80">
                    <span>{timelineChannelLabels(item, t)}</span>
                    {item.whatsapp_source === "business_app" ? (
                      <span>Business app</span>
                    ) : null}
                    <span>{formatMessageTime(item.created_at)}</span>
                    {item.sent_by_name ? <span>{item.sent_by_name}</span> : null}
                    {outbound && item.status === "failed" ? (
                      <span className="badge badge-canceled text-[10px] opacity-100">
                        {t("statusFailed")}
                      </span>
                    ) : null}
                  </div>
                  <p className="whitespace-pre-wrap">{item.body_text}</p>
                  {item.document_intake_job_id ? (
                    <p className="mt-1 text-xs opacity-80">
                      OCR #{item.document_intake_job_id}
                    </p>
                  ) : null}
                </div>
              </div>
            );
          })
        )}
      </div>

      <div className="space-y-2 rounded-lg border p-3">
        <p className="text-sm font-medium">{t("composeTitle")}</p>
        <div className="flex flex-wrap gap-2">
          {(["checkin", "reply", "custom"] as ComposeIntent[]).map((intent) => (
            <button
              key={intent}
              type="button"
              className={composeIntent === intent ? "btn btn-sm" : "btn-ghost btn-sm"}
              onClick={() => setComposeIntent(intent)}
              disabled={busy}
            >
              {intent === "checkin"
                ? t("intentCheckin")
                : intent === "reply"
                  ? t("intentReply")
                  : t("intentCustom")}
            </button>
          ))}
        </div>
        {composeIntent !== "checkin" ? (
          <label className="block text-sm">
            <span className="mb-1 block text-muted">{t("hintLabel")}</span>
            <input
              className="input w-full"
              value={composeHint}
              onChange={(event) => setComposeHint(event.target.value)}
              placeholder={composeIntent === "reply" ? t("hintReplyPlaceholder") : t("hintCustomPlaceholder")}
              disabled={busy}
            />
          </label>
        ) : null}
        <button type="button" className="btn btn-sm" onClick={() => void handleCompose()} disabled={busy}>
          {busy ? tc("loading") : t("composeAction")}
        </button>
      </div>

      {draftId !== null ? (
        <div className="space-y-2 rounded-lg border p-3">
          <label className="block text-sm">
            <span className="mb-1 block font-medium">{t("bodyLabel")}</span>
            <textarea
              className="input min-h-40 w-full"
              value={bodyText}
              onChange={(event) => setBodyText(event.target.value)}
              disabled={busy}
            />
          </label>

          {availableChannels.length > 0 ? (
            <div className="space-y-1">
              <p className="text-sm font-medium">{t("channelLabel")}</p>
              <div className="flex flex-wrap gap-2">
                {availableChannels.map((channel) => (
                  <label key={channel} className="inline-flex cursor-pointer items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name={`guest-message-channel-${reservationId}`}
                      value={channel}
                      checked={selectedChannel === channel}
                      onChange={() => setSelectedChannel(channel)}
                      disabled={busy}
                    />
                    {t(channelLabelKey(channel))}
                  </label>
                ))}
              </div>
              {selectedChannel ? (
                <p className="text-xs text-muted">
                  {channelHint(selectedChannel, channels, t, composeIntent) ?? null}
                </p>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-amber-800">{t("noChannel")}</p>
          )}

          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void handleSend()}
            disabled={busy || availableChannels.length === 0}
          >
            {busy ? tc("loading") : t("sendAction")}
          </button>
        </div>
      ) : null}

      {actionMessage ? <p className="text-sm text-green-700">{actionMessage}</p> : null}
      {error ? <p className="text-sm text-red-600">{error}</p> : null}
    </section>
  );
}
