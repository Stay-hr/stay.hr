import { channelBadgeClass } from "@/lib/reservationUi";
import type { ReservationChannel } from "@/lib/types";

type Props = {
  channel: ReservationChannel;
};

export function ReservationChannelBadge({ channel }: Props) {
  return (
    <span
      className={channelBadgeClass(channel.key)}
      title={channel.transport === "channex" ? "Channex" : undefined}
    >
      {channel.label}
    </span>
  );
}
