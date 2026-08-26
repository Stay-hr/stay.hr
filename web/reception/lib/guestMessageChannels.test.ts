import { describe, expect, it } from "vitest";
import type { GuestMessageChannels } from "@/lib/types";
import {
  availableGuestMessageChannels,
  preferredGuestMessageChannel,
} from "@/lib/guestMessageChannels";

const both: GuestMessageChannels = {
  email: { available: true, to: "guest@example.com" },
  whatsapp: { available: true, phone_wa: "38591111" },
  booking: { available: false },
  default_channel: "email",
  reply_channel: "whatsapp",
};

describe("preferredGuestMessageChannel", () => {
  it("prefers reply_channel over default_channel", () => {
    expect(
      preferredGuestMessageChannel(both, availableGuestMessageChannels(both)),
    ).toBe("whatsapp");
  });

  it("keeps the current channel when it is still available", () => {
    expect(
      preferredGuestMessageChannel(both, availableGuestMessageChannels(both), "email"),
    ).toBe("email");
  });

  it("falls back to default_channel when reply_channel is missing", () => {
    const channels: GuestMessageChannels = {
      ...both,
      reply_channel: "",
    };
    expect(
      preferredGuestMessageChannel(channels, availableGuestMessageChannels(channels)),
    ).toBe("email");
  });

  it("falls back to the first available channel", () => {
    const channels: GuestMessageChannels = {
      email: { available: false },
      whatsapp: { available: true },
      booking: { available: true },
    };
    expect(
      preferredGuestMessageChannel(channels, availableGuestMessageChannels(channels)),
    ).toBe("whatsapp");
  });

  it("returns empty when no channel is available", () => {
    const channels: GuestMessageChannels = {
      email: { available: false },
      whatsapp: { available: false },
      booking: { available: false },
    };
    expect(
      preferredGuestMessageChannel(channels, availableGuestMessageChannels(channels)),
    ).toBe("");
  });
});
