import type { IncomingReservationsCountResponse } from "@/lib/types";

export const INCOMING_COUNT_PATH = "/api/stay/reception/reservations/incoming-count/";
export const INCOMING_SEEN_STORAGE_KEY = "stay.reception.incomingSeenAt";
export const INCOMING_BADGE_POLL_MS = 60_000;

export type IncomingSeenStorage = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
};

function resolveStorage(storage?: IncomingSeenStorage | null): IncomingSeenStorage | null {
  if (storage === undefined) {
    if (typeof window === "undefined") return null;
    try {
      return window.localStorage;
    } catch {
      return null;
    }
  }
  return storage;
}

export function buildIncomingCountUrl(since: string | null): string {
  if (!since) return INCOMING_COUNT_PATH;
  const params = new URLSearchParams();
  params.set("since", since);
  return `${INCOMING_COUNT_PATH}?${params.toString()}`;
}

export function formatIncomingBadgeCount(count: number): string | null {
  if (!Number.isFinite(count) || count <= 0) return null;
  if (count > 99) return "99+";
  return String(Math.floor(count));
}

export function readIncomingSeenAt(storage?: IncomingSeenStorage | null): string | null {
  const store = resolveStorage(storage);
  if (!store) return null;
  try {
    const value = store.getItem(INCOMING_SEEN_STORAGE_KEY);
    const trimmed = value?.trim();
    return trimmed ? trimmed : null;
  } catch {
    return null;
  }
}

export function writeIncomingSeenAt(
  value: string,
  storage?: IncomingSeenStorage | null,
): boolean {
  const store = resolveStorage(storage);
  const trimmed = value.trim();
  if (!store || !trimmed) return false;
  try {
    store.setItem(INCOMING_SEEN_STORAGE_KEY, trimmed);
    return true;
  } catch {
    return false;
  }
}

export function applyIncomingSeenFromResponse(
  response: Pick<IncomingReservationsCountResponse, "latest_received_at"> | null | undefined,
  storage?: IncomingSeenStorage | null,
): string | null {
  const latest = response?.latest_received_at?.trim();
  if (!latest) {
    return readIncomingSeenAt(storage);
  }
  if (!writeIncomingSeenAt(latest, storage)) {
    return readIncomingSeenAt(storage);
  }
  return latest;
}

export function shouldRunIncomingBadgePoll(opts: {
  pathname: string;
  visibilityState: string;
}): boolean {
  if (opts.pathname === "/reservations/incoming") return false;
  if (opts.visibilityState === "hidden") return false;
  return true;
}
