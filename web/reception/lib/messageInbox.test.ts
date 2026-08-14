import { afterEach, describe, expect, it, vi } from "vitest";
import { extractHttpUrls, linkifiedAnchorProps } from "@/lib/linkifyText";
import {
  MESSAGE_INBOX_PAGE_SIZE,
  MESSAGE_THREADS_PATH,
  applyInboxFilterChange,
  buildMessageThreadsQuery,
  buildMessageThreadsUrl,
  buildNeedsReplyBadgeUrl,
  createInboxPollController,
  displayOrFallback,
  formatNeedsReplyBadgeCount,
  formatThreadChannelLabels,
  formatThreadMessageTime,
  handlePreviewUrlClick,
  inboxPagination,
  inboxViewState,
  buildReservationMessagesUrl,
  expandedIdIfVisible,
  isApiPreviewTruncated,
  isPreviewUrlClickTarget,
  isStaleTimelineResponse,
  nextExpandedId,
  previewClampClass,
  previewOverflows,
  shouldRefetchOpenTimeline,
  shouldResetPreviewExpanded,
  shouldShowPreviewToggle,
  shouldShowThreadChevron,
  shouldStoreTimelineCache,
  timelineCacheKey,
  reservationCardLinkProps,
  reservationMessagesHref,
  scrollToMessagesHash,
  shouldRunInboxPoll,
  shouldStartInboxFetch,
  uniqueThreadChannels,
} from "@/lib/messageInbox";

describe("message-threads request", () => {
  it("always includes sync=0 and never sync=1", () => {
    const cases = [
      buildMessageThreadsQuery(),
      buildMessageThreadsQuery({ page: 2, needsReply: true, arrivingToday: true }),
      buildMessageThreadsQuery({ pageSize: 1 }),
    ];
    for (const params of cases) {
      expect(params.get("sync")).toBe("0");
      expect(params.getAll("sync")).toEqual(["0"]);
      expect(params.toString()).not.toContain("sync=1");
    }
  });

  it("applies needs_reply and arriving_today individually and together", () => {
    expect(buildMessageThreadsQuery({ needsReply: true }).get("needs_reply")).toBe("1");
    expect(buildMessageThreadsQuery({ needsReply: true }).has("arriving_today")).toBe(false);

    expect(buildMessageThreadsQuery({ arrivingToday: true }).get("arriving_today")).toBe("1");
    expect(buildMessageThreadsQuery({ arrivingToday: true }).has("needs_reply")).toBe(false);

    const both = buildMessageThreadsQuery({ needsReply: true, arrivingToday: true });
    expect(both.get("needs_reply")).toBe("1");
    expect(both.get("arriving_today")).toBe("1");
    expect(both.get("sync")).toBe("0");
  });

  it("omits filters when they are off", () => {
    const params = buildMessageThreadsQuery({ needsReply: false, arrivingToday: false });
    expect(params.has("needs_reply")).toBe(false);
    expect(params.has("arriving_today")).toBe(false);
  });

  it("resets page to 1 when a filter changes", () => {
    const next = applyInboxFilterChange({ page: 4, needsReply: false, arrivingToday: false }, {
      needsReply: true,
    });
    expect(next.page).toBe(1);
    expect(next.needsReply).toBe(true);
    expect(next.arrivingToday).toBe(false);
  });

  it("paginates from backend total, not the current page length", () => {
    const { pages, canPrev, canNext } = inboxPagination(26, 1, MESSAGE_INBOX_PAGE_SIZE);
    expect(pages).toBe(2);
    expect(canPrev).toBe(false);
    expect(canNext).toBe(true);
    expect(inboxPagination(26, 2, 25).canNext).toBe(false);
    expect(inboxPagination(3, 1, 25).pages).toBe(1);
  });

  it("builds list and badge URLs against the existing endpoint", () => {
    expect(buildMessageThreadsUrl({ page: 2, needsReply: true })).toBe(
      `${MESSAGE_THREADS_PATH}?sync=0&page=2&page_size=25&needs_reply=1`,
    );
    const badge = new URL(buildNeedsReplyBadgeUrl(), "https://app.stay.hr");
    expect(badge.pathname).toBe("/api/stay/reception/message-threads/");
    expect(badge.searchParams.get("sync")).toBe("0");
    expect(badge.searchParams.get("page_size")).toBe("1");
  });
});

describe("reservation card link", () => {
  it("opens /reservations/{id}#messages in a new tab", () => {
    expect(reservationMessagesHref(798)).toBe("/reservations/798#messages");
    expect(reservationCardLinkProps(798)).toEqual({
      href: "/reservations/798#messages",
      target: "_blank",
      rel: "noopener noreferrer",
    });
  });
});

describe("preview URL click must not open the reservation", () => {
  it("stops propagation on preview URL clicks", () => {
    const stopPropagation = vi.fn();
    handlePreviewUrlClick({ stopPropagation });
    expect(stopPropagation).toHaveBeenCalledTimes(1);
  });

  it("detects preview URL targets and ignores the reservation header", () => {
    const url = {
      closest: (selector: string) => (selector === "[data-message-preview-url]" ? {} : null),
    };
    const header = {
      closest: () => null,
    };
    expect(isPreviewUrlClickTarget(url as unknown as EventTarget)).toBe(true);
    expect(isPreviewUrlClickTarget(header as unknown as EventTarget)).toBe(false);
    expect(isPreviewUrlClickTarget(null)).toBe(false);
  });
});

describe("thread channels", () => {
  it("shows unique last_channels without duplicates", () => {
    const channels = uniqueThreadChannels({
      last_channel: "booking",
      last_channels: ["booking", "whatsapp", "booking", "WhatsApp"],
    });
    expect(channels).toEqual(["booking", "whatsapp"]);
    expect(
      formatThreadChannelLabels(channels, (key) => {
        if (key === "channelBooking") return "Channex";
        if (key === "channelWhatsapp") return "WhatsApp";
        return "Mail";
      }),
    ).toBe("Channex · WhatsApp");
  });

  it("falls back to last_channel when last_channels is empty", () => {
    expect(uniqueThreadChannels({ last_channel: "email", last_channels: [] })).toEqual(["email"]);
  });
});

describe("needs_reply badge", () => {
  it("hides 0, shows a count, and caps at 99+", () => {
    expect(formatNeedsReplyBadgeCount(0)).toBeNull();
    expect(formatNeedsReplyBadgeCount(-1)).toBeNull();
    expect(formatNeedsReplyBadgeCount(1)).toBe("1");
    expect(formatNeedsReplyBadgeCount(12)).toBe("12");
    expect(formatNeedsReplyBadgeCount(99)).toBe("99");
    expect(formatNeedsReplyBadgeCount(100)).toBe("99+");
    expect(formatNeedsReplyBadgeCount(250)).toBe("99+");
  });
});

describe("display fallbacks", () => {
  it("never renders Invalid Date for a bad timestamp", () => {
    expect(formatThreadMessageTime("not-a-date")).toBe("not-a-date");
    expect(formatThreadMessageTime("")).toBe("");
    expect(formatThreadMessageTime(null)).toBe("");
    expect(formatThreadMessageTime("2026-06-05T17:20:00+00:00")).not.toContain("Invalid Date");
  });

  it("uses a tidy fallback for missing guest or room", () => {
    expect(displayOrFallback("", "—")).toBe("—");
    expect(displayOrFallback("   ", "—")).toBe("—");
    expect(displayOrFallback(null, "—")).toBe("—");
    expect(displayOrFallback("Daniela Heczko", "—")).toBe("Daniela Heczko");
  });
});

describe("inbox list states", () => {
  it("distinguishes loading, empty, and error without wiping a ready list", () => {
    expect(inboxViewState({ loading: true, error: "", threadCount: 0 })).toBe("loading");
    expect(inboxViewState({ loading: false, error: "", threadCount: 0 })).toBe("empty");
    expect(inboxViewState({ loading: false, error: "Failed", threadCount: 0 })).toBe("error");
    expect(inboxViewState({ loading: true, error: "", threadCount: 3 })).toBe("ready");
    expect(inboxViewState({ loading: false, error: "Failed", threadCount: 3 })).toBe("ready");
  });
});

describe("inbox polling", () => {
  it("runs only on /messages when visible and idle", () => {
    expect(
      shouldRunInboxPoll({ pathname: "/messages", visibilityState: "visible", requestInFlight: false }),
    ).toBe(true);
    expect(
      shouldRunInboxPoll({ pathname: "/", visibilityState: "visible", requestInFlight: false }),
    ).toBe(false);
    expect(
      shouldRunInboxPoll({ pathname: "/messages", visibilityState: "hidden", requestInFlight: false }),
    ).toBe(false);
    expect(
      shouldRunInboxPoll({ pathname: "/messages", visibilityState: "visible", requestInFlight: true }),
    ).toBe(false);
  });

  it("skips overlapping background fetches but allows a foreground filter/page load", () => {
    expect(shouldStartInboxFetch({ background: true, requestInFlight: true })).toBe(false);
    expect(shouldStartInboxFetch({ background: true, requestInFlight: false })).toBe(true);
    expect(shouldStartInboxFetch({ background: false, requestInFlight: true })).toBe(true);
  });

  it("stops after unmount (cleanup clears the interval)", () => {
    vi.useFakeTimers();
    const onTick = vi.fn();
    const stop = createInboxPollController({
      intervalMs: 45_000,
      shouldTick: () => true,
      onTick,
    });
    vi.advanceTimersByTime(45_000);
    expect(onTick).toHaveBeenCalledTimes(1);
    stop();
    vi.advanceTimersByTime(90_000);
    expect(onTick).toHaveBeenCalledTimes(1);
  });
});

describe("LinkifiedText http(s) only", () => {
  it("recognizes http(s) and opens them in a new tab", () => {
    const text = "See https://booking.uzorita.hr/check-in/abc and http://example.com/x.";
    expect(extractHttpUrls(text)).toEqual([
      "https://booking.uzorita.hr/check-in/abc",
      "http://example.com/x",
    ]);
    expect(linkifiedAnchorProps("https://booking.uzorita.hr/check-in/abc")).toMatchObject({
      href: "https://booking.uzorita.hr/check-in/abc",
      target: "_blank",
      rel: "noopener noreferrer",
    });
  });

  it("does not treat relative /reservations/123 as a link", () => {
    expect(extractHttpUrls("Open /reservations/123#messages please")).toEqual([]);
  });
});

describe("preview expand / collapse", () => {
  it("does not show a toggle when the clamped box has no overflow", () => {
    expect(previewOverflows({ scrollHeight: 48, clientHeight: 48 })).toBe(false);
    expect(shouldShowPreviewToggle({ overflows: false, expanded: false })).toBe(false);
  });

  it("shows the chevron when scrollHeight exceeds clientHeight", () => {
    expect(previewOverflows({ scrollHeight: 120, clientHeight: 48 })).toBe(true);
    expect(shouldShowPreviewToggle({ overflows: true, expanded: false })).toBe(true);
  });

  it("keeps the toggle after expand so the card can collapse again", () => {
    expect(shouldShowPreviewToggle({ overflows: true, expanded: true })).toBe(true);
    expect(previewClampClass(true)).not.toContain("line-clamp-3");
    expect(previewClampClass(false)).toContain("line-clamp-3");
  });

  it("uses accordion: one open id, same click closes", () => {
    expect(nextExpandedId(null, 10)).toBe(10);
    expect(nextExpandedId(10, 10)).toBeNull();
    expect(nextExpandedId(10, 20)).toBe(20);
  });

  it("shows a chevron on every valid reservation row, not overflow or preview text", () => {
    expect(shouldShowThreadChevron(798)).toBe(true);
    expect(shouldShowThreadChevron(0)).toBe(false);
    expect(shouldShowThreadChevron(Number.NaN)).toBe(false);
  });

  it("builds a DB-only timeline URL with sync=0", () => {
    const url = buildReservationMessagesUrl(798);
    expect(url).toBe("/api/stay/reception/reservations/798/messages/?sync=0");
    expect(url).not.toContain("sync=1");
  });

  it("refetches the open timeline only when last_message_at changes", () => {
    const key = timelineCacheKey(798, "2026-08-14T00:04:00+00:00");
    expect(shouldRefetchOpenTimeline(key, key)).toBe(false);
    expect(
      shouldRefetchOpenTimeline(key, timelineCacheKey(798, "2026-08-14T00:10:00+00:00")),
    ).toBe(true);
  });

  it("ignores a stale timeline response for a previous card or unmount", () => {
    expect(
      isStaleTimelineResponse({ requestId: 1, activeRequestId: 2, unmounted: false }),
    ).toBe(true);
    expect(
      isStaleTimelineResponse({ requestId: 2, activeRequestId: 2, unmounted: true }),
    ).toBe(true);
    expect(
      isStaleTimelineResponse({ requestId: 2, activeRequestId: 2, unmounted: false }),
    ).toBe(false);
  });

  it("does not store a failed timeline fetch as a cache hit", () => {
    expect(shouldStoreTimelineCache(true)).toBe(true);
    expect(shouldStoreTimelineCache(false)).toBe(false);
  });

  it("clears expandedId when the thread leaves the inbox page", () => {
    expect(expandedIdIfVisible(10, [10, 20])).toBe(10);
    expect(expandedIdIfVisible(10, [20, 30])).toBeNull();
    expect(expandedIdIfVisible(null, [10])).toBeNull();
  });

  it("resets expanded cards on page or filter change, not on poll", () => {
    const current = { page: 1, needsReply: false, arrivingToday: false };
    expect(shouldResetPreviewExpanded(current, current)).toBe(false);
    expect(shouldResetPreviewExpanded(current, { ...current, page: 2 })).toBe(true);
    expect(shouldResetPreviewExpanded(current, { ...current, needsReply: true })).toBe(true);
    expect(shouldResetPreviewExpanded(current, { ...current, arrivingToday: true })).toBe(true);
  });

  it("stops propagation so the toggle does not open the reservation", () => {
    const stopPropagation = vi.fn();
    handlePreviewUrlClick({ stopPropagation });
    expect(stopPropagation).toHaveBeenCalledTimes(1);
    expect(reservationCardLinkProps(798).href).toBe("/reservations/798#messages");
    expect(linkifiedAnchorProps("https://uzorita-sibenik.stay.hr/g/abc").target).toBe("_blank");
  });

  it("treats a 200-char API preview as truncated, not the full original message", () => {
    const preview = `${"x".repeat(197)}...`;
    expect(preview.length).toBe(200);
    expect(isApiPreviewTruncated(preview)).toBe(true);
    expect(isApiPreviewTruncated("Merci pour votre réservation !")).toBe(false);
  });
});

describe("scrollToMessagesHash", () => {
  it("scrolls only when the hash is #messages and the section exists", () => {
    const scrollIntoView = vi.fn();
    expect(scrollToMessagesHash("#other", () => ({ scrollIntoView }))).toBe(false);
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(scrollToMessagesHash("#messages", () => null)).toBe(false);
    expect(scrollToMessagesHash("#messages", () => ({ scrollIntoView }))).toBe(true);
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
  });
});

afterEach(() => {
  vi.useRealTimers();
});
