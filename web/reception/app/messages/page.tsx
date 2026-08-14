"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { LinkifiedText } from "@/lib/linkifyText";
import { formatStayDateRange } from "@/lib/locale-format";
import {
  MESSAGE_INBOX_PAGE_SIZE,
  MESSAGE_INBOX_POLL_MS,
  applyInboxFilterChange,
  buildMessageThreadsUrl,
  createInboxPollController,
  displayOrFallback,
  formatThreadChannelLabels,
  formatThreadMessageTime,
  inboxPagination,
  inboxViewState,
  reservationCardLinkProps,
  shouldRunInboxPoll,
  shouldStartInboxFetch,
  uniqueThreadChannels,
} from "@/lib/messageInbox";
import type { AppConfig, MessageThread, MessageThreadsListResponse } from "@/lib/types";

type InboxFilters = {
  page: number;
  needsReply: boolean;
  arrivingToday: boolean;
};

export default function MessagesInboxPage() {
  const t = useTranslations("messageInbox");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [tenantName, setTenantName] = useState("");
  const [threads, setThreads] = useState<MessageThread[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<InboxFilters>({
    page: 1,
    needsReply: false,
    arrivingToday: false,
  });
  const inflightRef = useRef(false);
  const requestSeqRef = useRef(0);

  useEffect(() => {
    void fetch("/api/stay/app/config")
      .then((res) => (res.ok ? res.json() : null))
      .then((config: AppConfig | null) => {
        if (config?.tenant?.name) setTenantName(config.tenant.name);
      })
      .catch(() => undefined);
  }, []);

  const loadThreads = useCallback(
    async (opts?: { background?: boolean }) => {
      const background = Boolean(opts?.background);
      if (!shouldStartInboxFetch({ background, requestInFlight: inflightRef.current })) return;
      const seq = ++requestSeqRef.current;
      inflightRef.current = true;
      if (!background) {
        setLoading(true);
        setError("");
      }
      try {
        const res = await fetch(
          buildMessageThreadsUrl({
            page: filters.page,
            pageSize: MESSAGE_INBOX_PAGE_SIZE,
            needsReply: filters.needsReply,
            arrivingToday: filters.arrivingToday,
          }),
        );
        if (!res.ok) throw new Error(t("loadFailed"));
        const data = (await res.json()) as MessageThreadsListResponse;
        if (seq !== requestSeqRef.current) return;
        setThreads(data.threads ?? []);
        setTotal(Number.isFinite(data.total) ? data.total : 0);
      } catch (err) {
        if (seq !== requestSeqRef.current) return;
        if (!background) {
          setError(err instanceof Error ? err.message : t("loadFailed"));
          setThreads([]);
          setTotal(0);
        }
      } finally {
        if (seq === requestSeqRef.current) {
          inflightRef.current = false;
          if (!background) {
            setLoading(false);
          }
        }
      }
    },
    [filters.arrivingToday, filters.needsReply, filters.page, t],
  );

  useEffect(() => {
    void loadThreads({ background: false });
  }, [loadThreads]);

  useEffect(() => {
    const stop = createInboxPollController({
      intervalMs: MESSAGE_INBOX_POLL_MS,
      shouldTick: () =>
        shouldRunInboxPoll({
          pathname: "/messages",
          visibilityState: document.visibilityState,
          requestInFlight: inflightRef.current,
        }),
      onTick: () => {
        void loadThreads({ background: true });
      },
    });
    const onVisibility = () => {
      if (
        shouldRunInboxPoll({
          pathname: "/messages",
          visibilityState: document.visibilityState,
          requestInFlight: inflightRef.current,
        })
      ) {
        void loadThreads({ background: true });
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [loadThreads]);

  const pagination = inboxPagination(total, filters.page, MESSAGE_INBOX_PAGE_SIZE);
  const listState = inboxViewState({ loading, error, threadCount: threads.length });
  const dash = tc("dash");

  return (
    <div className="min-h-screen">
      <ReceptionNav tenantName={tenantName} />
      <main className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-xl font-semibold text-stay-navy">{t("title")}</h1>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={filters.needsReply}
                onChange={(event) =>
                  setFilters((current) =>
                    applyInboxFilterChange(current, { needsReply: event.target.checked }),
                  )
                }
              />
              {t("filterNeedsReply")}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={filters.arrivingToday}
                onChange={(event) =>
                  setFilters((current) =>
                    applyInboxFilterChange(current, { arrivingToday: event.target.checked }),
                  )
                }
              />
              {t("filterArrivingToday")}
            </label>
            <button
              type="button"
              className="btn-ghost btn-sm"
              onClick={() => void loadThreads({ background: false })}
              disabled={loading}
            >
              {tc("refresh")}
            </button>
          </div>
        </div>

        {error ? <p className="text-sm text-red-600">{error}</p> : null}

        <div className="space-y-2">
          {listState === "loading" ? (
            <p className="text-sm text-muted">{tc("loading")}</p>
          ) : listState === "empty" ? (
            <p className="text-sm text-muted">{t("empty")}</p>
          ) : listState === "error" ? null : (
            threads.map((thread) => {
              const guest = displayOrFallback(thread.booker_name, dash);
              const room = displayOrFallback(thread.room_name, dash);
              const dates =
                formatStayDateRange(locale, thread.check_in ?? "", thread.check_out ?? "") ?? "";
              const channels = formatThreadChannelLabels(uniqueThreadChannels(thread), (key) =>
                t(key),
              );
              const cardLink = reservationCardLinkProps(thread.reservation_id);
              return (
                <article key={thread.reservation_id} className="card overflow-hidden">
                  <a
                    {...cardLink}
                    className="block space-y-1 p-3 transition hover:bg-slate-50"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-semibold text-stay-navy">{guest}</p>
                      {thread.needs_reply ? (
                        <span className="badge bg-red-600 text-white">{t("needsReply")}</span>
                      ) : null}
                      {thread.arrives_today ? (
                        <span className="badge bg-slate-100 text-slate-700">{t("arrivingToday")}</span>
                      ) : null}
                    </div>
                    <p className="text-sm text-muted">
                      {room}
                      {dates ? ` · ${dates}` : ""}
                    </p>
                    <p className="text-xs text-muted">
                      {channels}
                      {thread.last_message_at
                        ? ` · ${formatThreadMessageTime(thread.last_message_at)}`
                        : ""}
                    </p>
                    <span className="sr-only">{t("openReservation")}</span>
                  </a>
                  {thread.last_message_preview ? (
                    <div className="border-t border-stay-border px-3 py-2">
                      <LinkifiedText className="line-clamp-3 whitespace-pre-wrap text-sm text-muted">
                        {thread.last_message_preview}
                      </LinkifiedText>
                    </div>
                  ) : null}
                </article>
              );
            })
          )}
        </div>

        {total > 0 ? (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted">
              {t("pageStatus", { page: filters.page, pages: pagination.pages })}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                className="btn-ghost btn-sm"
                disabled={!pagination.canPrev || loading}
                onClick={() =>
                  setFilters((current) => ({ ...current, page: current.page - 1 }))
                }
              >
                {t("prevPage")}
              </button>
              <button
                type="button"
                className="btn-ghost btn-sm"
                disabled={!pagination.canNext || loading}
                onClick={() =>
                  setFilters((current) => ({ ...current, page: current.page + 1 }))
                }
              >
                {t("nextPage")}
              </button>
            </div>
          </div>
        ) : null}
      </main>
    </div>
  );
}
