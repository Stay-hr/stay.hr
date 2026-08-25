import { describe, expect, it } from "vitest";
import { extractApiError } from "@/lib/api-error";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("extractApiError", () => {
  it("prefers the reply field used by review POST validation", async () => {
    const res = jsonResponse(400, { reply: ["Booking.com does not allow replies to rating-only reviews."] });
    await expect(extractApiError(res, "Failed to send reply.")).resolves.toBe(
      "Booking.com does not allow replies to rating-only reviews.",
    );
  });

  it("reads detail when reply is absent", async () => {
    const res = jsonResponse(503, { detail: "Channex writes are disabled on this host." });
    await expect(extractApiError(res, "Failed to send reply.")).resolves.toBe(
      "Channex writes are disabled on this host.",
    );
  });

  it("reads a bare JSON array from legacy ValidationError(str)", async () => {
    const res = jsonResponse(400, ["Channex POST /reviews/x/reply failed (422)"]);
    await expect(extractApiError(res, "Failed to send reply.")).resolves.toBe(
      "Channex POST /reviews/x/reply failed (422)",
    );
  });

  it("falls back when the body is not JSON", async () => {
    const res = new Response("<html>oops</html>", { status: 500 });
    await expect(extractApiError(res, "Failed to send reply.")).resolves.toBe("Failed to send reply.");
  });
});
