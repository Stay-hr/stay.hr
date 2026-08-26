import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AiReplyWizardModal } from "@/app/_components/AiReplyWizardModal";
import type { GuestMessageComposeResult } from "@/lib/types";

const messages = {
  common: {
    loading: "Učitavanje…",
    error: "Greška",
    close: "Zatvori",
  },
  guestMessages: {
    composeFailed: "Generiranje poruke nije uspjelo.",
    composerCancel: "Odustani",
    aiWizardTitle: "AI odgovor",
    aiWizardStepInput: "Priprema",
    aiWizardQuestion: "Što želite da AI pripremi?",
    aiIntentReply: "Odgovor gostu",
    aiIntentReplyHelp: "AI odgovara na zadnju poruku gosta.",
    aiIntentCheckin: "Check-in poruka",
    aiIntentCheckinHelp: "AI priprema check-in poruku.",
    aiIntentCustom: "Druga poruka",
    aiIntentCustomHelp: "Korisnik zada što želi napisati.",
    aiHintLabel: "Dodatna uputa AI-u",
    aiHintPlaceholder: "Reci mu da možemo odobriti kasni check-in do 23:00.",
    aiGenerate: "Generiraj odgovor",
  },
};

const composeResponse = {
  draft_id: 77,
  body_text: "Hvala na poruci.",
  language: "hr",
  llm_used: true,
  channels: {
    email: { available: true, to: "guest@example.com" },
    whatsapp: { available: true },
    booking: { available: false },
    default_channel: "email",
    reply_channel: "whatsapp",
  },
};

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    headers: new Headers(),
  } as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AiReplyWizardModal", () => {
  it("calls compose once and hands the full result to onComplete", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(composeResponse, true, 201));
    vi.stubGlobal("fetch", fetchMock);
    const onComplete = vi.fn();

    render(
      <NextIntlClientProvider locale="hr" messages={messages}>
        <AiReplyWizardModal reservationId={1129} onClose={() => undefined} onComplete={onComplete} />
      </NextIntlClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Generiraj odgovor/ }));

    await waitFor(() => {
      expect(onComplete).toHaveBeenCalledTimes(1);
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/stay/reception/reservations/1129/messages/compose/");
    expect(JSON.parse(String(init.body))).toEqual({ intent: "reply" });

    const result = onComplete.mock.calls[0][0] as GuestMessageComposeResult;
    expect(result).toEqual({
      draftId: 77,
      bodyText: "Hvala na poruci.",
      channels: composeResponse.channels,
    });
    expect(result.draftId).not.toBeNull();
    expect(onComplete.mock.calls[0][1]).toBe("reply");
  });

  it("hides the extra hint for check-in and sends checkin intent", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(composeResponse, true, 201));
    vi.stubGlobal("fetch", fetchMock);
    const onComplete = vi.fn();

    render(
      <NextIntlClientProvider locale="hr" messages={messages}>
        <AiReplyWizardModal reservationId={1129} onClose={() => undefined} onComplete={onComplete} />
      </NextIntlClientProvider>,
    );

    expect(screen.getByPlaceholderText(/kasni check-in/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Check-in poruka/ }));
    expect(screen.queryByPlaceholderText(/kasni check-in/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Generiraj odgovor/ }));
    await waitFor(() => {
      expect(onComplete).toHaveBeenCalledTimes(1);
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ intent: "checkin" });
  });
});
