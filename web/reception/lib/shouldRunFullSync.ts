/**
 * Retired by ADR 0019 Phase A.
 *
 * Conversation GET is DB-only (`sync=0`). Provider fetch must not run on
 * panel mount, tab-visible, or interval. Realtime = reservation version
 * bump → `sync=0`. Explicit reconcile is Phase B (Celery / POST), not GET.
 */
export type FullSyncContext = {
  isMount?: boolean;
  hiddenAt: number | null;
  visibleAgain?: boolean;
  lastFullSyncAt: number | null;
  now: number;
};

/** Always false — do not issue blocking `sync=1` from the messages UI. */
export function shouldRunFullSync(_ctx: FullSyncContext): boolean {
  return false;
}
