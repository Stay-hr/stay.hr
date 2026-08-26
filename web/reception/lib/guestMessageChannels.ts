import type { GuestMessageChannels } from "@/lib/types";

export const GUEST_MESSAGE_CHANNEL_ORDER = ["email", "whatsapp", "booking"] as const;

export type GuestMessageChannelKey = (typeof GUEST_MESSAGE_CHANNEL_ORDER)[number];

export function availableGuestMessageChannels(
  channels: GuestMessageChannels,
): GuestMessageChannelKey[] {
  return GUEST_MESSAGE_CHANNEL_ORDER.filter((key) => channels[key]?.available);
}

export function preferredGuestMessageChannel(
  channels: GuestMessageChannels,
  available: readonly string[],
  current = "",
): string {
  if (available.length === 0) {
    return "";
  }
  if (current && available.includes(current)) {
    return current;
  }
  const reply = (channels.reply_channel || "").trim();
  if (reply && available.includes(reply)) {
    return reply;
  }
  const fallback = (channels.default_channel || "").trim();
  if (fallback && available.includes(fallback)) {
    return fallback;
  }
  return available[0] ?? "";
}

export function guestMessageChannelLabelKey(channel: string): string {
  if (channel === "booking") return "channelBooking";
  if (channel === "whatsapp") return "channelWhatsapp";
  return "channelEmail";
}

export function guestMessageChannelHint(
  channel: string,
  channels: GuestMessageChannels,
  t: (key: string, values?: Record<string, string>) => string,
  composeIntent?: string,
): string | null {
  if (channel === "booking" && channels.booking?.available) {
    return t("channelBookingHint");
  }
  if (channel === "email" && channels.email?.available && channels.email.to) {
    return t("channelEmailHint", { email: channels.email.to });
  }
  if (channel === "whatsapp" && channels.whatsapp?.available) {
    const wa = channels.whatsapp;
    if (wa.api_send && !wa.session_open) {
      const templateOk = composeIntent === "checkin" && Boolean(wa.template_available);
      if (!templateOk) {
        return t("channelWhatsappSessionClosedHint");
      }
      return t("channelWhatsappApiHint");
    }
    if (wa.api_send) {
      return t("channelWhatsappApiHint");
    }
    const phone = wa.phone_raw || wa.phone_wa || "";
    if (phone) {
      return t("channelWhatsappHint", { phone });
    }
  }
  return null;
}
