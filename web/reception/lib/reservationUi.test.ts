import { describe, expect, it } from "vitest";
import { channelBadgeClass } from "@/lib/reservationUi";

describe("channelBadgeClass", () => {
  it("returns a distinct class for known keys", () => {
    expect(channelBadgeClass("booking_com")).toContain("badge");
    expect(channelBadgeClass("airbnb")).not.toBe(channelBadgeClass("booking_com"));
    expect(channelBadgeClass("web")).not.toBe(channelBadgeClass("reception"));
  });

  it("falls back for unknown keys", () => {
    expect(channelBadgeClass("unknown")).toContain("badge");
  });
});
