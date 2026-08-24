import { describe, expect, it } from "vitest";
import {
  INCOMING_COUNT_PATH,
  INCOMING_SEEN_STORAGE_KEY,
  applyIncomingSeenFromResponse,
  buildIncomingCountUrl,
  formatIncomingBadgeCount,
  readIncomingSeenAt,
  shouldRunIncomingBadgePoll,
  writeIncomingSeenAt,
} from "@/lib/incomingSeen";

function memoryStorage(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    getItem(key: string) {
      return key in data ? data[key] : null;
    },
    setItem(key: string, value: string) {
      data[key] = value;
    },
    data,
  };
}

describe("buildIncomingCountUrl", () => {
  it("omits since when empty", () => {
    expect(buildIncomingCountUrl(null)).toBe(INCOMING_COUNT_PATH);
    expect(buildIncomingCountUrl("")).toBe(INCOMING_COUNT_PATH);
  });

  it("appends a since query param", () => {
    expect(buildIncomingCountUrl("2026-08-24T10:00:00+00:00")).toBe(
      `${INCOMING_COUNT_PATH}?since=${encodeURIComponent("2026-08-24T10:00:00+00:00")}`,
    );
  });
});

describe("formatIncomingBadgeCount", () => {
  it("hides 0, shows a count, and caps at 99+", () => {
    expect(formatIncomingBadgeCount(0)).toBeNull();
    expect(formatIncomingBadgeCount(-1)).toBeNull();
    expect(formatIncomingBadgeCount(Number.NaN)).toBeNull();
    expect(formatIncomingBadgeCount(1)).toBe("1");
    expect(formatIncomingBadgeCount(12)).toBe("12");
    expect(formatIncomingBadgeCount(99)).toBe("99");
    expect(formatIncomingBadgeCount(100)).toBe("99+");
  });
});

describe("incoming seen storage", () => {
  it("reads and writes the seen timestamp", () => {
    const storage = memoryStorage();
    expect(readIncomingSeenAt(storage)).toBeNull();
    expect(writeIncomingSeenAt("2026-08-24T10:00:00+00:00", storage)).toBe(true);
    expect(readIncomingSeenAt(storage)).toBe("2026-08-24T10:00:00+00:00");
    expect(storage.data[INCOMING_SEEN_STORAGE_KEY]).toBe("2026-08-24T10:00:00+00:00");
  });

  it("leaves existing seenAt untouched when setItem throws", () => {
    const existing = "2026-08-01T00:00:00+00:00";
    const storage = {
      getItem() {
        return existing;
      },
      setItem() {
        throw new Error("quota");
      },
    };
    expect(writeIncomingSeenAt("2026-08-24T10:00:00+00:00", storage)).toBe(false);
    expect(readIncomingSeenAt(storage)).toBe(existing);
  });
});

describe("applyIncomingSeenFromResponse", () => {
  it("writes latest_received_at only on a successful timestamp", () => {
    const storage = memoryStorage();
    expect(
      applyIncomingSeenFromResponse(
        { latest_received_at: "2026-08-24T12:00:00+00:00" },
        storage,
      ),
    ).toBe("2026-08-24T12:00:00+00:00");
    expect(readIncomingSeenAt(storage)).toBe("2026-08-24T12:00:00+00:00");
  });

  it("does not change existing seenAt when the response is null", () => {
    const storage = memoryStorage({
      [INCOMING_SEEN_STORAGE_KEY]: "2026-08-20T08:00:00+00:00",
    });
    expect(applyIncomingSeenFromResponse(null, storage)).toBe("2026-08-20T08:00:00+00:00");
    expect(readIncomingSeenAt(storage)).toBe("2026-08-20T08:00:00+00:00");
  });

  it("does not mark anything seen when latest_received_at is missing", () => {
    const storage = memoryStorage({
      [INCOMING_SEEN_STORAGE_KEY]: "2026-08-20T08:00:00+00:00",
    });
    expect(applyIncomingSeenFromResponse({ latest_received_at: null }, storage)).toBe(
      "2026-08-20T08:00:00+00:00",
    );
    expect(applyIncomingSeenFromResponse({ latest_received_at: "   " }, storage)).toBe(
      "2026-08-20T08:00:00+00:00",
    );
    expect(readIncomingSeenAt(storage)).toBe("2026-08-20T08:00:00+00:00");
  });
});

describe("shouldRunIncomingBadgePoll", () => {
  it("skips the incoming page and hidden tabs", () => {
    expect(
      shouldRunIncomingBadgePoll({
        pathname: "/reservations/incoming",
        visibilityState: "visible",
      }),
    ).toBe(false);
    expect(shouldRunIncomingBadgePoll({ pathname: "/", visibilityState: "hidden" })).toBe(false);
    expect(shouldRunIncomingBadgePoll({ pathname: "/", visibilityState: "visible" })).toBe(true);
  });
});
