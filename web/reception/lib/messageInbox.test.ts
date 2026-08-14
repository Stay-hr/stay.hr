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
  isPreviewUrlClickTarget,
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
