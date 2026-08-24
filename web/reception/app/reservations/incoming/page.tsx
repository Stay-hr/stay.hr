"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { CountryFlag } from "@/app/_components/CountryFlag";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { ReservationChannelBadge } from "@/app/_components/ReservationChannelBadge";
import { ReservationStayMeta } from "@/app/_components/ReservationStayMeta";
import { useMonthLabel, useReservationStatusLabel } from "@/lib/i18n-ui";
import { formatStayDateRange, stayNightsCount } from "@/lib/locale-format";
import { formatReservationRoomLine } from "@/lib/reservationRoomLabel";
import { reservationStatusClass } from "@/lib/reservationUi";
import type { Reservation } from "@/lib/types";
import { useSyncVersionsPoll } from "@/lib/useSyncVersionsPoll";
import { addDaysIso, formatArrivalTime, propertyDayIso, todayIso } from "@/lib/utils";

type IncomingPeriod = "today" | "days7" | "days30";

function periodRange(mode: IncomingPeriod): { from: string; to: string } {
  const today = todayIso();
  const to = addDaysIso(today, 1);
  if (mode === "today") {
    return { from: today, to };
  }
  if (mode === "days7") {
    return { from: addDaysIso(today, -6), to };
  }
  return { from: addDaysIso(today, -29), to };
}

export default function IncomingReservationsPage() {
  const t = useTranslations("incoming");
  const tc = useTranslations("common");
  const locale = useLocale();
  const statusLabel = useReservationStatusLabel();
  const monthLabel = useMonthLabel();
  const [tenantName, setTenantName] = useState("");
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState("");
  const [period, setPeriod] = useState<IncomingPeriod>("today");
  const [channelKey, setChannelKey] = useState("");
  const [search, setSearch] = useState("");

  const load = useCallback(
    async (opts?: { background?: boolean }) => {
      const background = Boolean(opts?.background);
      if (!background) {
        setInitialLoading(true);
      }
      setError("");
      try {
        const session = await fetch("/api/auth/session");
        if (session.ok) {
          const s = await session.json();
          setTenantName(s.tenant || "");
        }

        const { from, to } = periodRange(period);
        const params = new URLSearchParams();
        params.set("received_from", from);
        params.set("received_to", to);
        if (search.trim()) params.set("search", search.trim());

        const res = await fetch(`/api/stay/reception/reservations/?${params}`);
        if (!res.ok) throw new Error(t("loadFailed"));
        const data = (await res.json()) as Reservation[];
        setReservations(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : tc("error"));
      } finally {
        if (!background) {
          setInitialLoading(false);
        }
      }
    },
    [period, search, t, tc],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useSyncVersionsPoll({
    onStale: () => {
      void load({ background: true });
    },
  });

  const channelOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const reservation of reservations) {
      if (reservation.channel) {
        map.set(reservation.channel.key, reservation.channel.label);
      }
    }
    return [...map.entries()].sort((a, b) => a[1].localeCompare(b[1], locale));
  }, [locale, reservations]);

  const visible = useMemo(() => {
    if (!channelKey) return reservations;
    return reservations.filter((reservation) => reservation.channel?.key === channelKey);
  }, [channelKey, reservations]);

  const grouped = useMemo(() => {
    const map = new Map<string, Reservation[]>();
    for (const reservation of visible) {
      const stamp = reservation.received_at || reservation.booked_at || "";
      const key = stamp ? propertyDayIso(stamp) : "";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(reservation);
    }
    return [...map.entries()].sort(([a], [b]) => b.localeCompare(a));
  }, [visible]);

  return (
    <div>
      <ReceptionNav tenantName={tenantName} />
      <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
        <h1 className="text-lg font-semibold text-stay-navy">{t("title")}</h1>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <div className="label">{t("period")}</div>
            <select
              className="input mt-1 w-auto"
              value={period}
              onChange={(e) => setPeriod(e.target.value as IncomingPeriod)}
            >
              <option value="today">{t("periodToday")}</option>
              <option value="days7">{t("period7days")}</option>
              <option value="days30">{t("period30days")}</option>
            </select>
          </div>
          <div>
            <div className="label">{t("channel")}</div>
            <select
              className="input mt-1 w-auto"
              value={channelKey}
              onChange={(e) => setChannelKey(e.target.value)}
            >
              <option value="">{t("channelAll")}</option>
              {channelOptions.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
              {channelKey && !channelOptions.some(([key]) => key === channelKey) ? (
                <option value={channelKey}>{channelKey}</option>
              ) : null}
            </select>
          </div>
          <div className="min-w-[200px] flex-1">
            <div className="label">{t("search")}</div>
            <input
              className="input mt-1"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("searchPlaceholder")}
            />
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => {
              void load();
            }}
          >
            {tc("refresh")}
          </button>
        </div>

        {error ? <p className="text-sm text-red-600">{error}</p> : null}
        {initialLoading ? <p className="text-muted">{tc("loading")}</p> : null}

        {!initialLoading && grouped.length === 0 ? (
          <p className="text-muted">{t("noReservations")}</p>
        ) : null}

        {!initialLoading
          ? grouped.map(([day, items]) => (
              <section key={day || "unknown"} className="space-y-2">
                <h2 className="label">
                  {day ? `${monthLabel(day)} · ${t("receivedOn", { date: day })}` : t("receivedUnknown")}
                </h2>
                <ul className="space-y-2">
                  {items.map((r) => {
                    const roomLine = formatReservationRoomLine(r.room_name, r.room_codes);
                    const dateRangeLabel = formatStayDateRange(
                      locale,
                      r.check_in_date,
                      r.check_out_date,
                    );
                    const nights = stayNightsCount(r.check_in_date, r.check_out_date);
                    const receivedStamp = r.received_at || r.booked_at;
                    const receivedTime = receivedStamp ? formatArrivalTime(receivedStamp) : null;
                    return (
                      <li key={r.id}>
                        <Link
                          href={`/reservations/${r.id}`}
                          className="card card-hover flex flex-wrap items-center justify-between gap-3 px-4 py-3"
                        >
                          <div className="min-w-0 flex-1 space-y-0.5">
                            <div className="flex min-w-0 items-center gap-2 font-semibold text-stay-navy">
                              <CountryFlag iso2={r.primary_guest_nationality_iso2} />
                              <span className="min-w-0 break-words">
                                {r.primary_guest_name || r.room_name}
                              </span>
                            </div>
                            <div className="min-w-0 break-words text-sm text-muted">{roomLine}</div>
                            <ReservationStayMeta
                              dateRangeLabel={dateRangeLabel}
                              nightsLabel={
                                nights != null ? tc("nightsCount", { count: nights }) : null
                              }
                              guestsLabel={tc("guestsCount", { count: r.guests_count })}
                            />
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            {r.channel ? <ReservationChannelBadge channel={r.channel} /> : null}
                            {receivedTime ? (
                              <span className="w-[3.25rem] text-right text-sm tabular-nums text-muted">
                                {receivedTime}
                              </span>
                            ) : null}
                            <span className={`badge ${reservationStatusClass(r.status)}`}>
                              {statusLabel(r.status)}
                            </span>
                          </div>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))
          : null}
      </main>
    </div>
  );
}
