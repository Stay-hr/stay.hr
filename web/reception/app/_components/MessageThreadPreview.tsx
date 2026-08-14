"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { MessageBodyWithTranslate } from "@/app/_components/MessageBodyWithTranslate";
import { LinkifiedText } from "@/lib/linkifyText";
import {
  buildReservationMessagesUrl,
  formatThreadChannelLabels,
  formatThreadMessageTime,
  handlePreviewUrlClick,
  isStaleTimelineResponse,
  previewClampClass,
  shouldRefetchOpenTimeline,
  shouldShowThreadChevron,
  shouldStoreTimelineCache,
  threadWellId,
  timelineCacheKey,
  uniqueThreadChannels,
} from "@/lib/messageInbox";
import type { GuestMessageTimelineItem } from "@/lib/types";

type Props = {
  reservationId: number;
  preview: string;
  lastMessageAt: string | null;
  expanded: boolean;
  onToggle: () => void;
};

type TimelineCache = {
  key: string;
  items: GuestMessageTimelineItem[];
};

function ChevronIcon({ up }: { up: boolean }) {
  return (
    <svg
      viewBox="0 0 20 20"
      aria-hidden="true"
      className={`h-4 w-4 ${up ? "rotate-180" : ""}`}
    >
      <path
        fill="currentColor"
        d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06z"
      />
    </svg>
  );
}

function InboxTimelineBubbles({
  reservationId,
  items,
}: {
  reservationId: number;
  items: GuestMessageTimelineItem[];
}) {
  const t = useTranslations("messageInbox");
  return (
    <div className="max-h-80 space-y-2 overflow-y-auto">
      {items.map((item) => {
        const outbound = item.direction === "outbound";
        const channels = formatThreadChannelLabels(
          uniqueThreadChannels({
            last_channel: item.channel,
            last_channels: item.channels ?? [],
          }),
          (key) => t(key),
        );
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
                <span>{channels}</span>
                {item.whatsapp_source === "business_app" ? <span>Business app</span> : null}
                <span>{formatThreadMessageTime(item.created_at)}</span>
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
                linkClassName={outbound ? "text-white underline" : "text-stay-blue underline"}
                controlClassName={
                  outbound
                    ? "text-xs font-medium text-white/90 hover:underline"
                    : "text-xs font-medium text-stay-blue hover:underline"
                }
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function MessageThreadPreview({
  reservationId,
  preview,
  lastMessageAt,
  expanded,
  onToggle,
}: Props) {
  const t = useTranslations("messageInbox");
  const tc = useTranslations("common");
  const cacheRef = useRef<TimelineCache | null>(null);
  const requestIdRef = useRef(0);
  const [timeline, setTimeline] = useState<GuestMessageTimelineItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retryNonce, setRetryNonce] = useState(0);
  const wellId = threadWellId(reservationId);
  const showChevron = shouldShowThreadChevron(reservationId);
  const label = expanded ? t("collapsePreview") : t("expandPreview");

  useEffect(() => {
    if (!expanded) return;

    const key = timelineCacheKey(reservationId, lastMessageAt);
    const cached = cacheRef.current;
    if (cached && !shouldRefetchOpenTimeline(cached.key, key)) {
      setTimeline(cached.items);
      setError("");
      setLoading(false);
      return;
    }

    const requestId = ++requestIdRef.current;
    const controller = new AbortController();
    let unmounted = false;
    setLoading(true);
    setError("");

    void fetch(buildReservationMessagesUrl(reservationId), { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(t("timelineLoadFailed"));
        return res.json() as Promise<GuestMessageTimelineItem[]>;
      })
      .then((items) => {
        if (
          isStaleTimelineResponse({
            requestId,
            activeRequestId: requestIdRef.current,
            unmounted,
          })
        ) {
          return;
        }
        const rows = Array.isArray(items) ? items : [];
        if (shouldStoreTimelineCache(true)) {
          cacheRef.current = { key, items: rows };
        }
        setTimeline(rows);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (
          isStaleTimelineResponse({
            requestId,
            activeRequestId: requestIdRef.current,
            unmounted,
          })
        ) {
          return;
        }
        setError(err instanceof Error ? err.message : t("timelineLoadFailed"));
        setLoading(false);
      });

    return () => {
      unmounted = true;
      controller.abort();
    };
  }, [expanded, lastMessageAt, reservationId, retryNonce, t]);

  return (
    <div className="border-t border-stay-border">
      <div className="flex items-start gap-1 px-3 py-2">
        {preview ? (
          <div className={`min-w-0 flex-1 text-sm text-muted ${previewClampClass(false)}`}>
            <LinkifiedText className="whitespace-pre-wrap text-sm text-muted">{preview}</LinkifiedText>
          </div>
        ) : (
          <div className="min-w-0 flex-1" />
        )}
        {showChevron ? (
          <button
            type="button"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-stay-muted outline-none transition hover:bg-stay-blue-light hover:text-stay-blue focus-visible:ring-2 focus-visible:ring-stay-blue"
            aria-expanded={expanded}
            aria-controls={wellId}
            aria-label={label}
            title={label}
            onClick={(event) => {
              handlePreviewUrlClick(event);
              onToggle();
            }}
          >
            <ChevronIcon up={expanded} />
          </button>
        ) : null}
      </div>
      {expanded ? (
        <div id={wellId} className="space-y-2 border-t border-stay-border px-3 py-2">
          {loading && timeline.length === 0 ? (
            <p className="text-sm text-muted">{tc("loading")}</p>
          ) : error && timeline.length === 0 ? (
            <div className="space-y-2">
              <p className="text-sm text-red-600">{error}</p>
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={(event) => {
                  handlePreviewUrlClick(event);
                  setRetryNonce((n) => n + 1);
                }}
              >
                {t("retry")}
              </button>
            </div>
          ) : timeline.length === 0 ? (
            <p className="text-sm text-muted">{t("empty")}</p>
          ) : (
            <InboxTimelineBubbles reservationId={reservationId} items={timeline} />
          )}
        </div>
      ) : null}
    </div>
  );
}
