"use client";

import { useTranslations } from "next-intl";
import type { GuestMessageChannels, GuestMessageComposeIntent } from "@/lib/types";
import {
  availableGuestMessageChannels,
  guestMessageChannelHint,
  guestMessageChannelLabelKey,
} from "@/lib/guestMessageChannels";

type Props = {
  reservationId: number;
  bodyText: string;
  onBodyTextChange: (value: string) => void;
  channels: GuestMessageChannels;
  selectedChannel: string;
  onSelectedChannelChange: (channel: string) => void;
  composeIntent?: GuestMessageComposeIntent;
  busy: boolean;
  onSend: () => void;
  onCancel: () => void;
};

export function GuestMessageComposer({
  reservationId,
  bodyText,
  onBodyTextChange,
  channels,
  selectedChannel,
  onSelectedChannelChange,
  composeIntent,
  busy,
  onSend,
  onCancel,
}: Props) {
  const t = useTranslations("guestMessages");
  const tc = useTranslations("common");
  const availableChannels = availableGuestMessageChannels(channels);

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <label className="block text-sm">
        <span className="mb-1 block font-medium">{t("bodyLabel")}</span>
        <textarea
          className="input min-h-40 w-full"
          value={bodyText}
          onChange={(event) => onBodyTextChange(event.target.value)}
          disabled={busy}
        />
      </label>

      {availableChannels.length > 0 ? (
        <div className="space-y-1">
          <p className="text-sm font-medium">{t("channelLabel")}</p>
          <div className="flex flex-wrap gap-2">
            {availableChannels.map((channel) => (
              <label key={channel} className="inline-flex cursor-pointer items-center gap-2 text-sm">
                <input
                  type="radio"
                  name={`guest-message-channel-${reservationId}`}
                  value={channel}
                  checked={selectedChannel === channel}
                  onChange={() => onSelectedChannelChange(channel)}
                  disabled={busy}
                />
                {t(guestMessageChannelLabelKey(channel))}
              </label>
            ))}
          </div>
          {selectedChannel ? (
            <p className="text-xs text-muted">
              {guestMessageChannelHint(selectedChannel, channels, t, composeIntent) ?? null}
            </p>
          ) : null}
        </div>
      ) : (
        <p className="text-sm text-amber-800">{t("noChannel")}</p>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-sm"
          onClick={onSend}
          disabled={busy || availableChannels.length === 0}
        >
          {busy ? tc("loading") : t("sendAction")}
        </button>
        <button type="button" className="btn-ghost btn-sm" onClick={onCancel} disabled={busy}>
          {t("composerCancel")}
        </button>
      </div>
    </div>
  );
}
