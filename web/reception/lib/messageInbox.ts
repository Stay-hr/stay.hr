import type { MessageThread } from "@/lib/types";

export const MESSAGE_THREADS_PATH = "/api/stay/reception/message-threads/";
export const MESSAGE_INBOX_PAGE_SIZE = 25;
export const MESSAGE_INBOX_POLL_MS = 45_000;
export const MESSAGES_SECTION_ID = "messages";

export type MessageThreadsQuery = {
  page?: number;
  pageSize?: number;
  needsReply?: boolean;
  arrivingToday?: boolean;
};

export function buildMessageThreadsQuery(opts: MessageThreadsQuery = {}): URLSearchParams {
  const params = new URLSearchParams();
  params.set("sync", "0");
  params.set("page", String(opts.page && opts.page > 0 ? opts.page : 1));
  params.set(
    "page_size",
    String(opts.pageSize && opts.pageSize > 0 ? opts.pageSize : MESSAGE_INBOX_PAGE_SIZE),
  );
  if (opts.needsReply) params.set("needs_reply", "1");
  if (opts.arrivingToday) params.set("arriving_today", "1");
  return params;
}

export function buildMessageThreadsUrl(opts: MessageThreadsQuery = {}): string {
  return `${MESSAGE_THREADS_PATH}?${buildMessageThreadsQuery(opts).toString()}`;
}

/** Badge fetch: needs_reply_count only — do not load the inbox list. */
export function buildNeedsReplyBadgeUrl(): string {
  return buildMessageThreadsUrl({ page: 1, pageSize: 1 });
}

export function reservationMessagesHref(reservationId: number): string {
  return `/reservations/${reservationId}#${MESSAGES_SECTION_ID}`;
}

export function reservationCardLinkProps(reservationId: number): {
  href: string;
  target: "_blank";
  rel: "noopener noreferrer";
} {
  return {
    href: reservationMessagesHref(reservationId),
    target: "_blank",
    rel: "noopener noreferrer",
  };
}

export function formatNeedsReplyBadgeCount(count: number): string | null {
  if (!Number.isFinite(count) || count <= 0) return null;
  if (count > 99) return "99+";
  return String(Math.floor(count));
}

export function applyInboxFilterChange<T extends { page: number }>(
  current: T,
  patch: Partial<Omit<T, "page">>,
): T {
  return { ...current, ...patch, page: 1 };
}

export function inboxPageCount(total: number, pageSize: number): number {
  if (!Number.isFinite(total) || !Number.isFinite(pageSize) || total <= 0 || pageSize <= 0) {
    return 1;
  }
  return Math.ceil(total / pageSize);
}

export function inboxPagination(total: number, page: number, pageSize: number) {
  const pages = inboxPageCount(total, pageSize);
  const safePage = page > 0 ? page : 1;
  return {
    pages,
    canPrev: safePage > 1,
    canNext: safePage < pages,
  };
}

export function uniqueThreadChannels(thread: Pick<MessageThread, "last_channel" | "last_channels">): string[] {
  const raw =
    thread.last_channels?.length > 0
      ? thread.last_channels
      : thread.last_channel
        ? [thread.last_channel]
        : [];
  const seen = new Set<string>();
  const channels: string[] = [];
  for (const item of raw) {
    const key = (item ?? "").trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    channels.push(key);
  }
  return channels;
}

export function threadChannelLabelKey(channel: string): "channelBooking" | "channelWhatsapp" | "channelEmail" | null {
  if (channel === "booking") return "channelBooking";
  if (channel === "whatsapp") return "channelWhatsapp";
  if (channel === "email") return "channelEmail";
  return null;
}

export function formatThreadChannelLabels(
  channels: string[],
  label: (key: "channelBooking" | "channelWhatsapp" | "channelEmail") => string,
): string {
  return channels
    .map((channel) => {
      const key = threadChannelLabelKey(channel);
      return key ? label(key) : channel;
    })
    .join(" · ");
}

export function displayOrFallback(value: string | null | undefined, fallback: string): string {
  const trimmed = (value ?? "").trim();
  return trimmed || fallback;
}

export function formatThreadMessageTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function inboxViewState(opts: {
  loading: boolean;
  error: string;
  threadCount: number;
}): "loading" | "error" | "empty" | "ready" {
  if (opts.loading && opts.threadCount === 0) return "loading";
  if (opts.error && opts.threadCount === 0) return "error";
  if (opts.threadCount === 0) return "empty";
  return "ready";
}

export function shouldStartInboxFetch(opts: {
  background: boolean;
  requestInFlight: boolean;
}): boolean {
  if (opts.background && opts.requestInFlight) return false;
  return true;
}

export function shouldRunInboxPoll(opts: {
  pathname: string;
  visibilityState: string;
  requestInFlight: boolean;
}): boolean {
  if (opts.requestInFlight) return false;
  if (opts.pathname !== "/messages") return false;
  if (opts.visibilityState === "hidden") return false;
  return true;
}

export function handlePreviewUrlClick(event: { stopPropagation: () => void }): void {
  event.stopPropagation();
}

export function isPreviewUrlClickTarget(target: EventTarget | null): boolean {
  if (!target || typeof target !== "object") return false;
  const el = target as { closest?: (selector: string) => unknown };
  if (typeof el.closest !== "function") return false;
  return Boolean(el.closest("[data-message-preview-url]"));
}

export function scrollToMessagesHash(
  hash: string = typeof window === "undefined" ? "" : window.location.hash,
  getElement: (id: string) => { scrollIntoView: (opts?: ScrollIntoViewOptions) => void } | null = (id) =>
    typeof document === "undefined" ? null : document.getElementById(id),
): boolean {
  if (hash !== `#${MESSAGES_SECTION_ID}`) return false;
  const el = getElement(MESSAGES_SECTION_ID);
  if (!el) return false;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}

export function createInboxPollController(opts: {
  intervalMs?: number;
  shouldTick: () => boolean;
  onTick: () => void;
  setIntervalFn?: typeof setInterval;
  clearIntervalFn?: typeof clearInterval;
}): () => void {
  const intervalMs = opts.intervalMs ?? MESSAGE_INBOX_POLL_MS;
  const setIntervalFn = opts.setIntervalFn ?? setInterval;
  const clearIntervalFn = opts.clearIntervalFn ?? clearInterval;
  const id = setIntervalFn(() => {
    if (opts.shouldTick()) opts.onTick();
  }, intervalMs);
  return () => clearIntervalFn(id);
}
