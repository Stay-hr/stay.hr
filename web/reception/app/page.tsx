"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { BookingPdfImportForm } from "@/app/_components/BookingPdfImportForm";
import { CountryFlag } from "@/app/_components/CountryFlag";
import { EvisitorTimelineErrorModal } from "@/app/_components/EvisitorTimelineErrorModal";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import {
  timelineEvisitorBadgeClass,
  timelineEvisitorLabel,
  timelineEvisitorTone,
} from "@/lib/evisitorTimelineBadge";
import { useMonthLabel, useReservationStatusLabel } from "@/lib/i18n-ui";
import type { AppConfig, BookingPdfImportResult, Reservation } from "@/lib/types";
import { reservationStatusClass } from "@/lib/reservationUi";
import { singlePropertySlug } from "@/lib/app-config";
import { useSyncVersionsPoll } from "@/lib/useSyncVersionsPoll";
import {
  addDaysIso,
  addMonthsIso,
  startOfIsoWeekIso,
  startOfMonthIso,
  formatArrivalTime,
  todayIso,
} from "@/lib/utils";

type OverviewMode = "today" | "week" | "month" | "all";

export default function TimelinePage() {
  const router = useRouter();
  const t = useTranslations("timeline");
  const tc = useTranslations("common");
  const tr = useTranslations("reservation");
  const statusLabel = useReservationStatusLabel();
  const monthLabel = useMonthLabel();
  const [tenantName, setTenantName] = useState("");
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [overviewMode, setOverviewMode] = useState<OverviewMode>("today");
  const [properties, setProperties] = useState<Array<{ slug: string; name: string }>>([]);
  const [propertySlug, setPropertySlug] = useState("");
  const [evisitorErrorReservation, setEvisitorErrorReservation] = useState<Reservation | null>(
    null,
  );

  const load = useCallback(async (opts?: { background?: boolean }) => {
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

      const configRes = await fetch("/api/stay/app/config");
      if (configRes.ok) {
        const config = (await configRes.json()) as AppConfig;
        const nextProperties = config.properties ?? [];
        setProperties(nextProperties);
        setPropertySlug((current) => {
          if (current && nextProperties.some((property) => property.slug === current)) {
            return current;
          }
          const single = singlePropertySlug(config);
          if (single) return single;
          const uzorita = nextProperties.find((property) => property.slug === "uzorita");
          return uzorita?.slug ?? "";
        });
      }

      const today = todayIso();
      let periodFrom = "";
      let periodTo = "";
      if (overviewMode === "today") {
        periodFrom = today;
        periodTo = addDaysIso(today, 1);
      } else if (overviewMode === "week") {
        periodFrom = startOfIsoWeekIso(today);
        periodTo = addDaysIso(periodFrom, 7);
      } else if (overviewMode === "month") {
        periodFrom = startOfMonthIso(today);
        periodTo = addMonthsIso(periodFrom, 1);
      }

      const params = new URLSearchParams();
      if (status) params.set("status", status);
      if (search.trim()) params.set("search", search.trim());
      if (periodFrom) params.set("period_from", periodFrom);
      if (periodTo) params.set("period_to", periodTo);

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
  }, [overviewMode, search, status, t, tc]);

  useEffect(() => {
    void load();
  }, [load]);

  useSyncVersionsPoll({
    onStale: () => {
      void load({ background: true });
    },
  });

  const grouped = useMemo(() => {
    const map = new Map<string, Reservation[]>();
    for (const r of reservations) {
      const key = r.check_in_date;
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(r);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [reservations]);

  return (
    <div>
      <ReceptionNav tenantName={tenantName} />
      <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <div className="label">{t("period")}</div>
            <select
              className="input mt-1 w-auto"
              value={overviewMode}
              onChange={(e) => setOverviewMode(e.target.value as OverviewMode)}
            >
              <option value="today">{t("periodToday")}</option>
              <option value="week">{t("periodWeek")}</option>
              <option value="month">{t("periodMonth")}</option>
              <option value="all">{t("periodAll")}</option>
            </select>
          </div>
          <div>
            <div className="label">{t("status")}</div>
            <select className="input mt-1 w-auto" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">{t("statusAll")}</option>
              <option value="expected">{tr("statusExpected")}</option>
              <option value="pending">{tr("statusPending")}</option>
              <option value="checked_in">{tr("statusCheckedIn")}</option>
              <option value="checked_out">{tr("statusCheckedOut")}</option>
              <option value="canceled">{tr("statusCanceled")}</option>
              <option value="no_show">{tr("statusNoShow")}</option>
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
          <button type="button" className="btn" onClick={() => void load()}>
            {tc("refresh")}
          </button>
          {properties.length > 1 ? (
            <div>
              <div className="label">{t("property")}</div>
              <select
                className="input mt-1 w-auto"
                value={propertySlug}
                onChange={(e) => setPropertySlug(e.target.value)}
              >
                <option value="">{t("propertyPlaceholder")}</option>
                {properties.map((property) => (
                  <option key={property.slug} value={property.slug}>
                    {property.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
        </div>

        <BookingPdfImportForm
          propertySlug={propertySlug || undefined}
          onSuccess={(result: BookingPdfImportResult) => {
            void load();
            router.push(`/reservations/${result.id}`);
          }}
        />

        {error ? <p className="text-sm text-red-600">{error}</p> : null}
        {initialLoading ? <p className="text-muted">{tc("loading")}</p> : null}

        {!initialLoading && grouped.length === 0 ? <p className="text-muted">{t("noReservations")}</p> : null}

        <EvisitorTimelineErrorModal
          open={evisitorErrorReservation != null}
          reservation={evisitorErrorReservation}
          onClose={() => setEvisitorErrorReservation(null)}
        />

        {!initialLoading
          ? grouped.map(([day, items]) => (
          <section key={day} className="space-y-2">
            <h2 className="label">
              {monthLabel(day)} · {t("arrivalOn", { date: day })}
            </h2>
            <ul className="space-y-2">
              {items.map((r) => {
                // Arrival time is only operationally useful while still expected.
                const arrivalLabel =
                  r.status === "expected" && r.expected_arrival_at
                    ? formatArrivalTime(r.expected_arrival_at)
                    : null;
                const statedText = r.guest_stated_arrival_text?.trim() || "";
                const arrivalTitle =
                  statedText && statedText !== arrivalLabel ? statedText : undefined;
                const evisitorTone = timelineEvisitorTone(r);
                const evisitorProgress = r.evisitor_progress;
                const evisitorLabel =
                  evisitorTone && evisitorProgress
                    ? timelineEvisitorLabel(evisitorProgress)
                    : null;
                return (
                  <li key={r.id}>
                    <Link
                      href={`/reservations/${r.id}`}
                      className="card card-hover flex flex-wrap items-center justify-between gap-3 px-4 py-3"
                    >
                      <div>
                        <div className="flex items-center gap-2 font-semibold text-stay-navy">
                          <CountryFlag iso2={r.primary_guest_nationality_iso2} />
                          <span>{r.primary_guest_name || r.room_name}</span>
                        </div>
                        <div className="text-sm text-muted">
                          {r.room_name} · {r.check_in_date} → {r.check_out_date} ·{" "}
                          {tc("guestsCount", { count: r.guests_count })}
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <span
                          className="w-[3.25rem] text-right text-sm tabular-nums text-muted"
                          title={arrivalLabel ? arrivalTitle : undefined}
                        >
                          {arrivalLabel ?? ""}
                        </span>
                        {evisitorTone && evisitorLabel ? (
                          evisitorTone === "error" ? (
                            <button
                              type="button"
                              className={timelineEvisitorBadgeClass(evisitorTone)}
                              aria-label={t("evisitorErrorAria")}
                              onClick={(event) => {
                                event.preventDefault();
                                event.stopPropagation();
                                setEvisitorErrorReservation(r);
                              }}
                            >
                              {evisitorLabel}
                            </button>
                          ) : (
                            <span className={timelineEvisitorBadgeClass(evisitorTone)}>
                              {evisitorLabel}
                            </span>
                          )
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
