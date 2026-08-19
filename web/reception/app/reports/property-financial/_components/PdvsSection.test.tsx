import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PdvsSection,
  buildUploadExportFeedback,
} from "@/app/reports/property-financial/_components/PdvsSection";
import type { PdvsInvoice } from "@/lib/pdvsExport";
import hr from "@/messages/hr.json";

const uploadPdvsInvoice = vi.fn();
const downloadPdvsXml = vi.fn();
const downloadPdvXml = vi.fn();
const fetchPdvsInvoices = vi.fn();

vi.mock("@/lib/pdvsExport", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pdvsExport")>(
    "@/lib/pdvsExport",
  );
  return {
    ...actual,
    defaultPdvsPeriod: () => "2026-07",
    uploadPdvsInvoice: (...args: unknown[]) => uploadPdvsInvoice(...args),
    downloadPdvsXml: (...args: unknown[]) => downloadPdvsXml(...args),
    downloadPdvXml: (...args: unknown[]) => downloadPdvXml(...args),
    fetchPdvsInvoices: (...args: unknown[]) => fetchPdvsInvoices(...args),
  };
});

function invoice(overrides: Partial<PdvsInvoice> = {}): PdvsInvoice {
  return {
    id: 1,
    provider: "booking",
    invoice_number: "1657100253",
    tax_period: "2026-06",
    taxable_amount: "69.48",
    currency: "EUR",
    supplier_name: "Booking.com B.V.",
    supplier_country: "NL",
    supplier_vat_id: "805734958B01",
    invoice_date: "2026-06-30",
    imported_at: "2026-06-30T12:00:00Z",
    created: true,
    ...overrides,
  };
}

function emptyList(period = "2026-07") {
  return {
    configured: true,
    missing: [],
    warnings: [],
    period,
    count: 0,
    results: [] as PdvsInvoice[],
  };
}

function listWith(inv: PdvsInvoice) {
  return {
    configured: true,
    missing: [],
    warnings: [],
    period: inv.tax_period,
    count: 1,
    results: [inv],
  };
}

function renderSection() {
  return render(
    <NextIntlClientProvider locale="hr" messages={hr}>
      <PdvsSection />
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  fetchPdvsInvoices.mockResolvedValue(emptyList());
  downloadPdvsXml.mockResolvedValue(undefined);
  downloadPdvXml.mockResolvedValue(undefined);
});

describe("buildUploadExportFeedback", () => {
  const tFn = (key: string, values?: Record<string, string>) => {
    const map: Record<string, string> = {
      uploadSuccess: "Uvezen račun {number}.",
      alreadyImported: "Račun je već uvezen {at}.",
      exportBothSuccess: "PDV-S i PDV XML preuzeti.",
      exportPartialPdvsOnly: "PDV-S XML preuzet; PDV XML nije uspio.",
      exportPartialPdvOnly: "PDV XML preuzet; PDV-S XML nije uspio.",
      exportBothFailed:
        "Račun je uvezen, ali izvoz PDV-S i PDV XML nije uspio. Koristite gumbe za ponovni izvoz.",
      exportError: "Izvoz PDV-S XML-a nije uspio.",
      exportPdvError: "Izvoz PDV XML-a nije uspio.",
    };
    let s = map[key] ?? key;
    if (values) {
      for (const [k, v] of Object.entries(values)) {
        s = s.replace(`{${k}}`, v);
      }
    }
    return s;
  };

  it("reports both exports ok", () => {
    const fb = buildUploadExportFeedback({
      t: tFn,
      duplicate: false,
      invoiceNumber: "1",
      importedAtLabel: "",
      pdvsOk: true,
      pdvOk: true,
    });
    expect(fb.error).toBe("");
    expect(fb.message).toContain("Uvezen račun 1");
    expect(fb.message).toContain("PDV-S i PDV XML preuzeti");
  });

  it("keeps already-imported tone for duplicates", () => {
    const fb = buildUploadExportFeedback({
      t: tFn,
      duplicate: true,
      invoiceNumber: "1",
      importedAtLabel: "1. 6. 2026.",
      pdvsOk: false,
      pdvOk: false,
    });
    expect(fb.message).toContain("već uvezen");
    expect(fb.message).not.toContain("Uvezen račun");
    expect(fb.error).toContain("izvoz PDV-S i PDV XML nije uspio");
  });
});

describe("PdvsSection", () => {
  it("shows a visible upload button", async () => {
    renderSection();
    const upload = await screen.findByRole("button", {
      name: "Učitaj Booking PDF",
    });
    expect(upload.className).toContain("btn");
    expect((upload as HTMLButtonElement).disabled).toBe(false);
  });

  it("uses tax_period from upload response for both exports", async () => {
    const inv = invoice({ tax_period: "2026-06" });
    uploadPdvsInvoice.mockResolvedValue(inv);
    fetchPdvsInvoices
      .mockResolvedValueOnce(emptyList("2026-07"))
      .mockResolvedValueOnce(listWith(inv));

    renderSection();
    await screen.findByRole("button", { name: "Učitaj Booking PDF" });

    fireEvent.change(screen.getByTestId("pdvs-file-input"), {
      target: {
        files: [new File(["pdf"], "booking.pdf", { type: "application/pdf" })],
      },
    });

    await waitFor(() => {
      expect(downloadPdvsXml).toHaveBeenCalledWith("2026-06");
      expect(downloadPdvXml).toHaveBeenCalledWith("2026-06");
    });
    expect(downloadPdvsXml).not.toHaveBeenCalledWith("2026-07");
  });

  it("does not export when upload fails", async () => {
    uploadPdvsInvoice.mockRejectedValue(new Error("bad pdf"));
    renderSection();
    await screen.findByRole("button", { name: "Učitaj Booking PDF" });

    fireEvent.change(screen.getByTestId("pdvs-file-input"), {
      target: {
        files: [new File(["x"], "bad.pdf", { type: "application/pdf" })],
      },
    });

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("bad pdf");
    });
    expect(downloadPdvsXml).not.toHaveBeenCalled();
    expect(downloadPdvXml).not.toHaveBeenCalled();
  });

  it("still attempts the other export when one fails", async () => {
    const inv = invoice();
    uploadPdvsInvoice.mockResolvedValue(inv);
    fetchPdvsInvoices
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValueOnce(listWith(inv));
    downloadPdvsXml.mockRejectedValue(new Error("pdvs fail"));
    downloadPdvXml.mockResolvedValue(undefined);

    renderSection();
    await screen.findByRole("button", { name: "Učitaj Booking PDF" });

    fireEvent.change(screen.getByTestId("pdvs-file-input"), {
      target: {
        files: [new File(["x"], "ok.pdf", { type: "application/pdf" })],
      },
    });

    await waitFor(() => {
      expect(downloadPdvsXml).toHaveBeenCalled();
      expect(downloadPdvXml).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(/PDV XML preuzet/);
      expect(screen.getByRole("alert").textContent).toMatch(/PDV-S/);
    });
  });

  it("disables upload while busy so a second file change is ignored", async () => {
    let resolveUpload: (value: PdvsInvoice) => void = () => undefined;
    uploadPdvsInvoice.mockImplementation(
      () =>
        new Promise<PdvsInvoice>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    fetchPdvsInvoices.mockResolvedValue(emptyList());

    renderSection();
    const uploadBtn = await screen.findByRole("button", {
      name: "Učitaj Booking PDF",
    });
    const input = screen.getByTestId("pdvs-file-input");

    fireEvent.change(input, {
      target: {
        files: [new File(["a"], "1.pdf", { type: "application/pdf" })],
      },
    });

    await waitFor(() => expect((uploadBtn as HTMLButtonElement).disabled).toBe(true));
    expect(uploadPdvsInvoice).toHaveBeenCalledTimes(1);

    fireEvent.change(input, {
      target: {
        files: [new File(["b"], "2.pdf", { type: "application/pdf" })],
      },
    });
    expect(uploadPdvsInvoice).toHaveBeenCalledTimes(1);

    const inv = invoice();
    fetchPdvsInvoices.mockResolvedValue(listWith(inv));
    resolveUpload(inv);

    await waitFor(() => expect((uploadBtn as HTMLButtonElement).disabled).toBe(false));
  });

  it("shows already-imported copy for duplicate response, not new-upload success", async () => {
    const inv = invoice({
      already_imported: true,
      created: false,
      imported_at: "2026-06-15T10:00:00Z",
    });
    uploadPdvsInvoice.mockResolvedValue(inv);
    fetchPdvsInvoices
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValueOnce(listWith(inv));

    renderSection();
    await screen.findByRole("button", { name: "Učitaj Booking PDF" });

    fireEvent.change(screen.getByTestId("pdvs-file-input"), {
      target: {
        files: [new File(["x"], "dup.pdf", { type: "application/pdf" })],
      },
    });

    await waitFor(() => {
      const status = screen.getByRole("status").textContent ?? "";
      expect(status).toMatch(/već uvezen/i);
      expect(status).not.toMatch(/^Uvezen račun/);
    });
  });
});
