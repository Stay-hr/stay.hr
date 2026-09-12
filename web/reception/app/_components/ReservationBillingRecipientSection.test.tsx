import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReservationBillingRecipientSection } from "@/app/_components/ReservationBillingRecipientSection";
import type { OpenBillingRecipient } from "@/lib/billingRecipient";
import hr from "@/messages/hr.json";

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    headers: new Headers(),
  } as Response;
}

function readyRow(overrides: Partial<OpenBillingRecipient> = {}): OpenBillingRecipient {
  return {
    id: 9,
    reservation_id: 42,
    status: "ready",
    identity_confidence: "unverified",
    company_name: "Example GmbH",
    tax_id: "DE123456789",
    tax_id_country: "DE",
    country: "DE",
    address: "Unter den Linden 1",
    postal_code: "10115",
    city: "Berlin",
    email: "billing@example.com",
    phone: "",
    requested_at: "2026-09-12T10:00:00Z",
    ready_at: "2026-09-12T10:05:00Z",
    ...overrides,
  };
}

function renderSection() {
  return render(
    <NextIntlClientProvider locale="hr" messages={hr}>
      <ReservationBillingRecipientSection reservationId={42} />
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ReservationBillingRecipientSection", () => {
  it("POSTs the first save after GET 404 and never sends status", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "Open billing recipient not found." }, false, 404))
      .mockResolvedValueOnce(
        jsonResponse(readyRow({ status: "requested", ready_at: null }), true, 201),
      );
    vi.stubGlobal("fetch", fetchMock);

    renderSection();
    expect(await screen.findByRole("heading", { name: "Podaci za račun na firmu" })).toBeTruthy();
    expect(screen.queryByText("Nedostaju podaci")).toBeNull();
    expect(screen.queryByText("Spremno za račun")).toBeNull();
    expect(screen.queryByRole("button", { name: /applied|primijeni/i })).toBeNull();

    fireEvent.change(screen.getByLabelText("Naziv firme"), {
      target: { value: "Example GmbH" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Spremi" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/api/stay/reception/reservations/42/billing-recipient/");
    expect(init.method).toBe("POST");
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body).not.toHaveProperty("status");
    expect(body).not.toHaveProperty("identity_confidence");
    expect(body.company_name).toBe("Example GmbH");
  });

  it("shows READY as informational and PATCHes later edits", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(readyRow()))
      .mockResolvedValueOnce(jsonResponse(readyRow({ city: "Hamburg" })));
    vi.stubGlobal("fetch", fetchMock);

    renderSection();
    expect(await screen.findByText("Spremno za račun")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Grad"), { target: { value: "Hamburg" } });
    fireEvent.click(screen.getByRole("button", { name: "Spremi" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(init.method).toBe("PATCH");
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body.city).toBe("Hamburg");
    expect(body).not.toHaveProperty("status");
    expect(body).not.toHaveProperty("identity_confidence");
  });

  it("shows REQUESTED as missing details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(readyRow({ status: "requested", ready_at: null, email: "" })),
      ),
    );
    renderSection();
    expect(await screen.findByText("Nedostaju podaci")).toBeTruthy();
    expect(screen.queryByText("Spremno za račun")).toBeNull();
  });

  it("blocks a Croatian OIB that is not 11 digits without writing", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "Open billing recipient not found." }, false, 404));
    vi.stubGlobal("fetch", fetchMock);

    renderSection();
    await screen.findByLabelText("Naziv firme");
    fireEvent.change(screen.getByLabelText("Naziv firme"), {
      target: { value: "Example d.o.o." },
    });
    fireEvent.change(screen.getByLabelText("Država poreznog broja"), {
      target: { value: "HR" },
    });
    fireEvent.change(screen.getByLabelText("OIB / VAT ID"), {
      target: { value: "1234567890" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Spremi" }));

    expect(await screen.findByText("HR OIB mora imati 11 znamenki.")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toBeUndefined();
  });
});
