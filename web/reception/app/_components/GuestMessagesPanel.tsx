"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { extractApiError } from "@/lib/api-error";
import {
  ensureCorrelationId,
  logGuestMessageEvent,
  sanitizeBody,
  syncCorrelationIdFromResponse,
} from "@/lib/guestMessageDebug";
import {
  availableGuestMessageChannels,
  guestMessageChannelHint,
  guestMessageChannelLabelKey,
  preferredGuestMessageChannel,
} from "@/lib/guestMessageChannels";
import type {
  GuestMessageChannels,
  GuestMessageComposeIntent,
  GuestMessageComposeResponse,
  GuestMessageComposeResult,
  GuestMessageTimelineItem,
} from "@/lib/types";
import { AiReplyWizardModal } from "@/app/_components/AiReplyWizardModal";
import { GuestMessageComposer } from "@/app/_components/GuestMessageComposer";
import { MessageBodyWithTranslate } from "@/app/_components/MessageBodyWithTranslate";
import { MessageTranslateCacheProvider } from "@/app/_components/MessageTranslateCacheProvider";
import { MESSAGES_SECTION_ID, scrollToMessagesHash } from "@/lib/messageInbox";
import { useReservationVersionWatch } from "@/lib/useReservationVersionWatch";

type Props = {
  reservationId: number;
};

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

function timelineChannelLabels(
  item: GuestMessageTimelineItem,
  t: (key: string) => string,
): string {
  const channels = item.channels?.length ? item.channels : [item.channel];
  return channels.map((channel) => t(guestMessageChannelLabelKey(channel))).join(" · ");
}

export function GuestMessagesPanel({ reservationId }: Props) {
  const t = useTranslations("guestMessages");
  const tc = useTranslations("common");
  const [timeline, setTimeline] = useState<GuestMessageTimelineItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [aiWizardOpen, setAiWizardOpen] = useState(false);
  const [composeIntent, setComposeIntent] = useState<GuestMessageComposeIntent>("reply");
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
    () => availableGuestMessageChannels(channels),
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

  const loadChannels = useCallback(async () => {
    try {
      const res = await fetch(`${baseUrl}/channels/`);
      if (!res.ok) return;
      const data = (await res.json()) as GuestMessageChannels;
      setChannels(data);
    } catch {
      // Channel radios stay empty until compose or a later refresh.
    }
  }, [baseUrl]);

  useEffect(() => {
    void loadTimeline({ background: false });
    void loadChannels();
    setComposerOpen(false);
    setAiWizardOpen(false);
    setDraftId(null);
    setBodyText("");
    setComposeIntent("reply");
  }, [reservationId, loadTimeline, loadChannels]);

  useEffect(() => {
    scrollToMessagesHash();
  }, [reservationId]);

  useEffect(() => {
    if (loading) return;
    const frame = window.requestAnimationFrame(() => {
      scrollToMessagesHash();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [loading]);

  useReservationVersionWatch({
    reservationId,
    scope: "messages",
    transport: "poll",
    onVersionChange: () => {
      void loadTimeline({ background: true });
    },
  });

  useEffect(() => {
    setSelectedChannel((current) =>
      preferredGuestMessageChannel(channels, availableChannels, current),
    );
  }, [availableChannels, channels]);

  useEffect(() => {
    if (!correlationId || !selectedChannel || draftId === null) return;
    if (channelLoggedRef.current === `${correlationId}:${selectedChannel}`) return;
    channelLoggedRef.current = `${correlationId}:${selectedChannel}`;
    logGuestMessageEvent("channel.change", {
      correlationId,
      selectedChannel,
      defaultChannel: channels.default_channel ?? "",
      channelHint: guestMessageChannelHint(selectedChannel, channels, t, composeIntent),
    });
  }, [correlationId, selectedChannel, channels, draftId, t, composeIntent]);

  function openManualComposer() {
    setError("");
    setActionMessage("");
    setAiWizardOpen(false);
    setDraftId(null);
    setBodyText("");
    setComposeIntent("reply");
    setComposerOpen(true);
  }

  function handleAiComplete(result: GuestMessageComposeResult, intent: GuestMessageComposeIntent) {
    setDraftId(result.draftId);
    setBodyText(result.bodyText);
    setChannels(result.channels);
    setSelectedChannel("");
    setComposeIntent(intent);
    setAiWizardOpen(false);
    setComposerOpen(true);
    setActionMessage(t("composeReady"));
    setError("");
  }

  function closeComposer() {
    if (busy) return;
    setComposerOpen(false);
    setDraftId(null);
    setBodyText("");
    setActionMessage("");
  }

  async function handleDismissReply() {
    setBusy(true);
    setError("");
    setActionMessage("");
    try {
      const res = await fetch(`${baseUrl}/dismiss-reply/`, { method: "POST" });
      if (!res.ok) {
        throw new Error(await extractApiError(res, t("dismissReplyFailed")));
      }
      setActionMessage(t("dismissReplyDone"));
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      setBusy(false);
    }
  }

  async function handleSend() {
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
    try {
      let sendDraftId = draftId;
      if (sendDraftId === null) {
        const composeRes = await fetch(`${baseUrl}/compose/`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Correlation-Id": cid,
          },
          body: JSON.stringify({ body_text: text, hint: "manual" }),
        });
        const echoedCompose = syncCorrelationIdFromResponse(composeRes, cid);
        setCorrelationId(echoedCompose);
        if (!composeRes.ok) {
          throw new Error(await extractApiError(composeRes, t("composeFailed")));
        }
        const composed = (await composeRes.json()) as GuestMessageComposeResponse;
        sendDraftId = composed.draft_id;
        setDraftId(sendDraftId);
      }

      logGuestMessageEvent("send.start", {
        correlationId: cid,
        reservationId,
        draftId: sendDraftId,
        selectedChannel,
        ...sanitizeBody(text),
      });

      const res = await fetch(`${baseUrl}/send/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Correlation-Id": cid,
        },
        body: JSON.stringify({
          draft_id: sendDraftId,
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
          draftId: sendDraftId,
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
            draftId: sendDraftId,
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
        draftId: sendDraftId,
        selectedChannel,
        send_ms: sendMs,
        status: data && "status" in data ? data.status : null,
        handoff_reason: data && "handoff_reason" in data ? data.handoff_reason ?? null : null,
        wa_me_url: data && "wa_me_url" in data ? data.wa_me_url ?? null : null,
        provider_message_id:
          data && "provider_message_id" in data ? data.provider_message_id ?? null : null,
        popupBlocked,
        ...sanitizeBody(text),
      });

      setDraftId(null);
      setBodyText("");
      setComposerOpen(false);
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
    <MessageTranslateCacheProvider>
      <section id={MESSAGES_SECTION_ID} className="scroll-mt-4 space-y-3">
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
                  <MessageBodyWithTranslate
                    reservationId={reservationId}
                    item={item}
                    className="whitespace-pre-wrap"
                    linkClassName={
                      outbound ? "text-white underline" : "text-stay-blue underline"
                    }
                    controlClassName={
                      outbound
                        ? "text-xs font-medium text-white/90 hover:underline"
                        : "text-xs font-medium text-stay-blue hover:underline"
                    }
                  />
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

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-sm"
          onClick={openManualComposer}
          disabled={busy}
        >
          {t("actionManual")}
        </button>
        <button
          type="button"
          className="btn-ghost btn-sm"
          onClick={() => {
            setError("");
            setAiWizardOpen(true);
          }}
          disabled={busy}
        >
          <span aria-hidden>✨ </span>
          {t("actionAi")}
        </button>
        <button
          type="button"
          className="btn-ghost btn-sm"
          onClick={() => void handleDismissReply()}
          disabled={busy}
        >
          {t("dismissReply")}
        </button>
      </div>

      {composerOpen ? (
        <GuestMessageComposer
          reservationId={reservationId}
          bodyText={bodyText}
          onBodyTextChange={setBodyText}
          channels={channels}
          selectedChannel={selectedChannel}
          onSelectedChannelChange={setSelectedChannel}
          composeIntent={composeIntent}
          busy={busy}
          onSend={() => void handleSend()}
          onCancel={closeComposer}
        />
      ) : null}

      {aiWizardOpen ? (
        <AiReplyWizardModal
          reservationId={reservationId}
          onClose={() => setAiWizardOpen(false)}
          onComplete={handleAiComplete}
        />
      ) : null}

      {actionMessage ? <p className="text-sm text-green-700">{actionMessage}</p> : null}
      {error ? <p className="text-sm text-red-600">{error}</p> : null}
      </section>
    </MessageTranslateCacheProvider>
  );
}
