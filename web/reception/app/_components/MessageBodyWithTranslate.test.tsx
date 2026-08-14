import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageBodyWithTranslate } from "@/app/_components/MessageBodyWithTranslate";
import { MessageTranslateCacheProvider } from "@/app/_components/MessageTranslateCacheProvider";
import type { GuestMessageTimelineItem } from "@/lib/types";

const messages = {
  guestMessages: {
    translateAction: "Prevedi",
    showOriginal: "Prikaži original",
    showTranslation: "Prikaži prijevod",
    translatedLabel: "Automatski prijevod (OpenAI)",
    translateFailed: "Prijevod nije uspio.",
    retry: "Pokušaj ponovno",
  },
};

const item: GuestMessageTimelineItem = {
  id: 42,
  source: "whatsapp",
  direction: "inbound",
  channel: "whatsapp",
  body_text: "Hello",
  created_at: "2026-08-14T10:00:00Z",
  status: null,
  sent_by_name: null,
  from_email: null,
  wa_me_url: null,
};

const itemWithUrl: GuestMessageTimelineItem = {
  ...item,
  body_text: "Hello https://stay.hr",
};

function Harness({
  children,
  locale = "hr",
}: {
  children: ReactNode;
  locale?: string;
}) {
  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      <MessageTranslateCacheProvider>{children}</MessageTranslateCacheProvider>
    </NextIntlClientProvider>
  );
}

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: async () => body,
  } as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("MessageBodyWithTranslate", () => {
  it("sends one POST with JSON timeline_id and lang", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        timeline_id: 42,
        original: "Hello",
        translated: "Bok",
        target_lang: "hr",
        is_translated: true,
        from_cache: false,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <Harness>
        <MessageBodyWithTranslate reservationId={7} item={item} />
      </Harness>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));

    await screen.findByText("Bok");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/stay/reception/reservations/7/messages/translate/");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(String(init.body))).toEqual({ timeline_id: 42, lang: "hr" });
  });

  it("disables the control and sets aria-busy while loading", async () => {
    let resolveJson!: (value: unknown) => void;
    const fetchMock = vi.fn().mockReturnValue(
      Promise.resolve({
        ok: true,
        json: () =>
          new Promise((resolve) => {
            resolveJson = resolve;
          }),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <Harness>
        <MessageBodyWithTranslate reservationId={7} item={item} />
      </Harness>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    const busy = await screen.findByRole("button", { name: "Prevedi" });
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    expect(busy.getAttribute("aria-busy")).toBe("true");

    resolveJson({
      timeline_id: 42,
      original: "Hello",
      translated: "Bok",
      target_lang: "hr",
      is_translated: true,
      from_cache: false,
    });
    expect(await screen.findByText("Automatski prijevod (OpenAI)")).toBeTruthy();
  });

  it("shows translated text and label, then toggles original without a new POST", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        timeline_id: 42,
        original: "Hello",
        translated: "Bok",
        target_lang: "hr",
        is_translated: true,
        from_cache: false,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <Harness>
        <MessageBodyWithTranslate reservationId={7} item={item} />
      </Harness>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    expect(await screen.findByText("Bok")).toBeTruthy();
    expect(screen.getByText("Automatski prijevod (OpenAI)")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Prikaži original" }));
    expect(screen.getByText("Hello")).toBeTruthy();
    expect(screen.queryByText("Automatski prijevod (OpenAI)")).toBeNull();
    expect(screen.getByRole("button", { name: "Prikaži prijevod" })).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the original and removes controls when is_translated is false", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          timeline_id: 42,
          original: "Hello",
          translated: "Hello",
          target_lang: "hr",
          is_translated: false,
          from_cache: false,
        }),
      ),
    );

    render(
      <Harness>
        <MessageBodyWithTranslate reservationId={7} item={item} />
      </Harness>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Prevedi" })).toBeNull();
    });
    expect(screen.getByText("Hello")).toBeTruthy();
    expect(screen.queryByText("Automatski prijevod (OpenAI)")).toBeNull();
    expect(screen.queryByRole("button", { name: "Prikaži prijevod" })).toBeNull();
  });

  it("shows an inline error and retries the same request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({}, false))
      .mockResolvedValueOnce(
        jsonResponse({
          timeline_id: 42,
          original: "Hello",
          translated: "Bok",
          target_lang: "hr",
          is_translated: true,
          from_cache: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <Harness>
        <MessageBodyWithTranslate reservationId={7} item={item} />
      </Harness>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    expect(await screen.findByText("Prijevod nije uspio.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Pokušaj ponovno" }));
    expect(await screen.findByText("Bok")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(String((fetchMock.mock.calls[1] as [string, RequestInit])[1].body))).toEqual({
      timeline_id: 42,
      lang: "hr",
    });
  });

  it("stops propagation on translate, toggle, and URL clicks", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        timeline_id: 42,
        original: "Hello",
        translated: "Bok",
        target_lang: "hr",
        is_translated: true,
        from_cache: false,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const parentClick = vi.fn();

    render(
      <Harness>
        <div onClick={parentClick}>
          <MessageBodyWithTranslate reservationId={7} item={itemWithUrl} />
        </div>
      </Harness>,
    );

    fireEvent.click(screen.getByRole("link", { name: "https://stay.hr" }));
    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prikaži original" }));
    expect(parentClick).not.toHaveBeenCalled();
  });

  it("aborts the shared request on provider unmount without showing an error", async () => {
    let signal: AbortSignal | undefined;
    const fetchMock = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      signal = init.signal ?? undefined;
      return new Promise(() => undefined);
    });
    vi.stubGlobal("fetch", fetchMock);

    function MountControl() {
      const [open, setOpen] = useState(true);
      return (
        <div>
          <button type="button" onClick={() => setOpen(false)}>
            close
          </button>
          {open ? (
            <Harness>
              <MessageBodyWithTranslate reservationId={7} item={item} />
            </Harness>
          ) : null}
        </div>
      );
    }

    render(<MountControl />);
    fireEvent.click(screen.getByRole("button", { name: "Prevedi" }));
    await waitFor(() => {
      expect(signal).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "close" }));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByText("Prijevod nije uspio.")).toBeNull();
  });

  it("does not show Prevedi for an empty body", () => {
    render(
      <Harness>
        <MessageBodyWithTranslate reservationId={7} item={{ ...item, body_text: "   " }} />
      </Harness>,
    );
    expect(screen.queryByRole("button", { name: "Prevedi" })).toBeNull();
  });
});
