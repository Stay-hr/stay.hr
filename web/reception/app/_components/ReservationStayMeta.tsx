import type { ReactNode } from "react";

type Props = {
  dateRangeLabel: string | null;
  nightsLabel?: string | null;
  guestsLabel?: string | null;
};

function MetaIcon({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex shrink-0 text-muted" aria-hidden>
      {children}
    </span>
  );
}

function CalendarIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M3 10h18M8 3v4M16 3v4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M21 14.5A8.5 8.5 0 0 1 9.5 3 7 7 0 1 0 21 14.5z" />
    </svg>
  );
}

function UsersIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 19c0-3 2.7-5 6.5-5s6.5 2 6.5 5" />
      <circle cx="17" cy="9" r="2.5" />
      <path d="M15.5 19c.4-1.8 1.8-3.2 3.9-3.7" />
    </svg>
  );
}

/** Presentational stay meta row — parents pass preformatted labels only. */
export function ReservationStayMeta({ dateRangeLabel, nightsLabel, guestsLabel }: Props) {
  if (!dateRangeLabel && !nightsLabel && !guestsLabel) return null;

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
      {dateRangeLabel ? (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <MetaIcon>
            <CalendarIcon />
          </MetaIcon>
          <span className="min-w-0 break-words">{dateRangeLabel}</span>
        </span>
      ) : null}
      {nightsLabel ? (
        <span className="inline-flex shrink-0 items-center gap-1.5">
          <MetaIcon>
            <MoonIcon />
          </MetaIcon>
          <span>{nightsLabel}</span>
        </span>
      ) : null}
      {guestsLabel ? (
        <span className="inline-flex shrink-0 items-center gap-1.5">
          <MetaIcon>
            <UsersIcon />
          </MetaIcon>
          <span>{guestsLabel}</span>
        </span>
      ) : null}
    </div>
  );
}
