import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  MessageTranslateSession,
  buildTranslatePayload,
  buildTranslateUrl,
  canTranslateBody,
  isStaleTranslateResponse,
  shouldClearPendingEntry,
  translateCacheKey,
  translateDisplayState,
  viewModeStorageKey,
  type TranslateResult,
} from "@/lib/messageTranslate";

const okResult: TranslateResult = {
  timeline_id: 42,
  original: "Hello",
  translated: "Bok",
  target_lang: "hr",
  is_translated: true,
  from_cache: false,
};

const sameLangResult: TranslateResult = {
  ...okResult,
  translated: "Hello",
  is_translated: false,
};

describe("translate request identity", () => {
  it("builds URL and payload with timeline_id plus lang only", () => {
    expect(buildTranslateUrl(7)).toBe(
      "/api/stay/reception/reservations/7/messages/translate/",
    );
    const payload = buildTranslatePayload({ id: 42 }, "hr");
    expect(payload).toEqual({ timeline_id: 42, lang: "hr" });
    expect(Object.keys(payload)).toEqual(["timeline_id", "lang"]);
    expect(payload).not.toHaveProperty("canonical_id");
    expect(payload).not.toHaveProperty("body_text");
  });
});

describe("translate cache key and view mode", () => {
  it("includes reservation, timeline id, and lang", () => {
    expect(translateCacheKey({ reservationId: 1, timelineId: 2, lang: "hr" })).toBe(
      "1:2:hr",
    );
    expect(translateCacheKey({ reservationId: 1, timelineId: 2, lang: "de" })).toBe(
      "1:2:de",
    );
  });

  it("stores view mode on the same key as the result", () => {
    const input = { reservationId: 9, timelineId: 8, lang: "en" };
    expect(viewModeStorageKey(input)).toBe(translateCacheKey(input));
  });
});

describe("canTranslateBody", () => {
  it("rejects empty or whitespace-only bodies", () => {
    expect(canTranslateBody("")).toBe(false);
    expect(canTranslateBody("   ")).toBe(false);
    expect(canTranslateBody(null)).toBe(false);
    expect(canTranslateBody("Bok")).toBe(true);
  });
});

describe("translateDisplayState", () => {
  it("hides all translate chrome when is_translated is false", () => {
    expect(translateDisplayState(sameLangResult)).toEqual({
      hideChrome: true,
      canShowTranslation: false,
    });
  });

  it("allows translation chrome when is_translated is true", () => {
    expect(translateDisplayState(okResult)).toEqual({
      hideChrome: false,
      canShowTranslation: true,
    });
  });
});

describe("isStaleTranslateResponse", () => {
  it("treats unmount and locale change as stale", () => {
    expect(
      isStaleTranslateResponse({ requestLang: "hr", activeLang: "hr", unmounted: true }),
    ).toBe(true);
    expect(
      isStaleTranslateResponse({ requestLang: "hr", activeLang: "de", unmounted: false }),
    ).toBe(true);
    expect(
      isStaleTranslateResponse({ requestLang: "hr", activeLang: "hr", unmounted: false }),
    ).toBe(false);
  });
});

describe("shouldClearPendingEntry", () => {
  it("clears only the same pending request", () => {
    const pending = {
      status: "pending" as const,
      requestId: 1,
      promise: Promise.resolve(okResult),
      abort: new AbortController(),
    };
    expect(shouldClearPendingEntry(pending, 1)).toBe(true);
    expect(shouldClearPendingEntry({ ...pending, requestId: 2 }, 1)).toBe(false);
    expect(shouldClearPendingEntry({ status: "ok", result: okResult }, 1)).toBe(false);
    expect(shouldClearPendingEntry(undefined, 1)).toBe(false);
  });
});

describe("MessageTranslateSession", () => {
  it("reuses one in-flight promise per key", async () => {
    const session = new MessageTranslateSession();
    let starts = 0;
    let resolveStart!: (value: TranslateResult) => void;
    const start = () => {
      starts += 1;
      return new Promise<TranslateResult>((resolve) => {
        resolveStart = resolve;
      });
    };

    const first = session.getOrCreate("1:42:hr", start);
    const second = session.getOrCreate("1:42:hr", start);
    expect(starts).toBe(1);
    expect(second).toBe(first);
    expect(session.getOk("1:42:hr")).toBeUndefined();

    resolveStart(okResult);
    await expect(first).resolves.toEqual(okResult);
    await expect(second).resolves.toEqual(okResult);
    expect(session.getOk("1:42:hr")).toEqual(okResult);
    expect(session.getViewMode("1:42:hr")).toBe("translated");
  });

  it("does not treat a failed request as a cache hit", async () => {
    const session = new MessageTranslateSession();
    await expect(
      session.getOrCreate("1:42:hr", () => Promise.reject(new Error("fail"))),
    ).rejects.toThrow("fail");
    expect(session.getOk("1:42:hr")).toBeUndefined();

    const start = () => Promise.resolve(okResult);
    await expect(session.getOrCreate("1:42:hr", start)).resolves.toEqual(okResult);
    expect(session.getOk("1:42:hr")).toEqual(okResult);
  });

  it("keeps a newer ok entry when an older pending would have cleared itself", () => {
    const olderPending = {
      status: "pending" as const,
      requestId: 1,
      promise: Promise.resolve(okResult),
      abort: new AbortController(),
    };
    const newerOk = { status: "ok" as const, result: okResult };
    expect(shouldClearPendingEntry(olderPending, 1)).toBe(true);
    expect(shouldClearPendingEntry(newerOk, 1)).toBe(false);
  });

  it("keeps view mode after a later result read", async () => {
    const session = new MessageTranslateSession();
    await session.getOrCreate("1:42:hr", () => Promise.resolve(okResult));
    session.setViewMode("1:42:hr", "original");
    expect(session.getViewMode("1:42:hr")).toBe("original");
    expect(session.getOk("1:42:hr")).toEqual(okResult);
  });

  it("stores is_translated false as ok so remount does not POST again", async () => {
    const session = new MessageTranslateSession();
    let starts = 0;
    await session.getOrCreate("1:42:hr", () => {
      starts += 1;
      return Promise.resolve(sameLangResult);
    });
    expect(session.getOk("1:42:hr")).toEqual(sameLangResult);
    expect(session.getViewMode("1:42:hr")).toBe("original");
    expect(translateDisplayState(session.getOk("1:42:hr"))).toEqual({
      hideChrome: true,
      canShowTranslation: false,
    });

    await session.getOrCreate("1:42:hr", () => {
      starts += 1;
      return Promise.resolve(sameLangResult);
    });
    expect(starts).toBe(1);
  });
});

describe("shared body renderer", () => {
  it("is the only message body renderer in inbox and reservation chat", () => {
    const root = path.resolve(__dirname, "..");
    const panel = readFileSync(path.join(root, "app/_components/GuestMessagesPanel.tsx"), "utf8");
    const preview = readFileSync(
      path.join(root, "app/_components/MessageThreadPreview.tsx"),
      "utf8",
    );
    expect(panel).toContain("MessageBodyWithTranslate");
    expect(preview).toContain("MessageBodyWithTranslate");
    expect(panel).not.toContain("LinkifiedText");
    expect(preview).toMatch(/InboxTimelineBubbles[\s\S]*MessageBodyWithTranslate/);
    expect(preview).toMatch(/preview[\s\S]*<LinkifiedText/);
  });
});
