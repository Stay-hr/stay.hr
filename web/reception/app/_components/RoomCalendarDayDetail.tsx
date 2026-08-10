"use client";

import { useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { CountryFlag } from "@/app/_components/CountryFlag";
import { ReservationDetailPanel } from "@/app/_components/ReservationDetailPanel";
import { ReservationStayMeta } from "@/app/_components/ReservationStayMeta";
import { overlapsDay } from "@/lib/calendarLayout";
import { formatChannelRateValue } from "@/lib/channelCalendarAri";
import {
  formatObpTierList,
} from "@/lib/obpPricing";
import {
  freeUnitsForNight,
  isDayTappable,
  isUnitFreeForRange,
} from "@/lib/calendarAvailability";
import { reservationHasChannelBlock, standaloneBlocks } from "@/lib/calendarBlocks";
import { useMonthLabel, useReservationStatusLabel } from "@/lib/i18n-ui";
import { formatStayDateRange, stayNightsCount } from "@/lib/locale-format";
import { reservationStatusClass } from "@/lib/reservationUi";
import type {
  CalendarBlock,
  CalendarReservation,
  CalendarSelection,
  ChannelRateDay,
  Room,
} from "@/lib/types";
import type { BulkWizardPrefill } from "@/app/_components/ChannelBulkWizardModal";
import { addDaysIso } from "@/lib/utils";

type Props = {
  selection: CalendarSelection | null;
  room: Room | null;
  rooms: Room[];
  reservations: CalendarReservation[];
  byRoom: Record<number, CalendarReservation[]>;
  blocks: CalendarBlock[];
  onChanged: () => void | Promise<void>;
  showCalendarBlocks?: boolean;
  showChannelAri?: boolean;
  channelAvailability?: Record<number, Record<string, number>>;
  channelRates?: Record<number, Record<string, ChannelRateDay[]>>;
  onOpenBulkWizard?: (prefill: BulkWizardPrefill) => void;
};

export function RoomCalendarDayDetail({
  selection,
  room,
  rooms,
  reservations,
  byRoom,
  blocks,
  onChanged,
  showCalendarBlocks = true,
  showChannelAri = false,
  channelAvailability = {},
  channelRates = {},
  onOpenBulkWizard,
}: Props) {
  const t = useTranslations("calendar");
  const tc = useTranslations("common");
  const ts = useTranslations("source");
  const locale = useLocale();
  const statusLabel = useReservationStatusLabel();
  const monthLabel = useMonthLabel();

  const [selectedUnitIds, setSelectedUnitIds] = useState<Set<number>>(new Set());
  const [blockCheckIn, setBlockCheckIn] = useState("");
  const [blockCheckOut, setBlockCheckOut] = useState("");
  const [busy, setBusy] = useState(false);
  const [busyMessage, setBusyMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [expandedReservationId, setExpandedReservationId] = useState<number | null>(null);

  useEffect(() => {
    if (!selection?.date) return;
    setBlockCheckIn(selection.date);
    setBlockCheckOut(addDaysIso(selection.date, 1));
    setSelectedUnitIds(new Set());
    setActionError("");
    setExpandedReservationId(null);
  }, [selection?.date, selection?.roomId]);

  const anchorDate = selection?.date ?? "";
  const freeUnits = useMemo(() => {
    if (!anchorDate) return [];
    return freeUnitsForNight(rooms, anchorDate, byRoom, blocks);
  }, [rooms, anchorDate, byRoom, blocks]);

  if (!selection || !room) {
    return (
      <section className="card p-4">
        <p className="text-sm text-muted">{t("clickDayHint")}</p>
      </section>
    );
  }

  const dayReservations = reservations.filter((r) =>
    overlapsDay(r.check_in_date, r.check_out_date, selection.date),
  );
  const dayBlocks = standaloneBlocks(
    blocks.filter(
      (b) => b.unit_id === room.id && overlapsDay(b.check_in, b.check_out, selection.date),
    ),
  );

  const dayAvailability = channelAvailability[room.id]?.[selection.date];
  const dayRateRows = (channelRates[room.id]?.[selection.date] ?? []).slice().sort((a, b) =>
    a.rate_plan_code.localeCompare(b.rate_plan_code),
  );
  const hasChannelData = dayAvailability !== undefined || dayRateRows.length > 0;

  const canBlock = isDayTappable(selection.date);
  const hasItems = dayReservations.length > 0 || dayBlocks.length > 0;

  function toggleUnit(unitId: number) {
    setSelectedUnitIds((prev) => {
      const next = new Set(prev);
      if (next.has(unitId)) next.delete(unitId);
      else next.add(unitId);
      return next;
    });
  }

  async function handleUnblock(block: CalendarBlock) {
    if (!block.can_unblock || block.id == null) return;
    if (!window.confirm(t("unblockConfirm", { unit: block.unit_code }))) return;

    setBusy(true);
    setBusyMessage(t("unblocking", { unit: block.unit_code }));
    setActionError("");
    try {
      const res = await fetch(`/api/stay/reception/blocks/${block.id}/`, { method: "DELETE" });
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(data?.detail || t("blockActionFailed"));
      }
      await onChanged();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : t("blockActionFailed"));
    } finally {
      setBusy(false);
      setBusyMessage("");
    }
  }

  async function handleBlockSubmit() {
    if (selectedUnitIds.size === 0 || !blockCheckIn || !blockCheckOut) return;
    if (!window.confirm(t("blockConfirm", { from: blockCheckIn, to: blockCheckOut }))) return;

    const unitIds = [...selectedUnitIds];
    const invalid = unitIds.filter(
      (id) => !isUnitFreeForRange(id, blockCheckIn, blockCheckOut, byRoom, blocks),
    );
    if (invalid.length > 0) {
      setActionError(t("blockRangeUnavailable"));
      return;
    }

    setBusy(true);
    setActionError("");
    const failed: string[] = [];
    try {
      for (let i = 0; i < unitIds.length; i += 1) {
        const unitId = unitIds[i];
        const unit = rooms.find((r) => r.id === unitId);
        setBusyMessage(t("blockingProgress", { unit: unit?.code ?? String(unitId), current: i + 1, total: unitIds.length }));
        const res = await fetch(`/api/stay/reception/units/${unitId}/block/`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ check_in: blockCheckIn, check_out: blockCheckOut }),
        });
        if (!res.ok) {
          const data = (await res.json().catch(() => null)) as { detail?: string } | null;
          failed.push(unit?.code ?? String(unitId));
          if (data?.detail) setActionError(String(data.detail));
        }
      }
      if (failed.length === 0) {
        setSelectedUnitIds(new Set());
        await onChanged();
      } else {
        setActionError(t("blockPartialFailed", { units: failed.join(", ") }));
        await onChanged();
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : t("blockActionFailed"));
    } finally {
      setBusy(false);
      setBusyMessage("");
    }
  }

  return (
    <section className="space-y-3">
      {busy ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="card max-w-sm p-4 text-center text-sm">{busyMessage || tc("loading")}</div>
        </div>
      ) : null}

      <h2 className="text-sm font-semibold text-stay-navy">
        {room.code} — {room.room_type_name} · {monthLabel(selection.date)} · {selection.date}
      </h2>

      {!hasItems ? (
        <div className="card p-4">
          <p className="text-sm text-muted">{t("noItemsForDay")}</p>
        </div>
      ) : (
        <ul className="space-y-2">
          {dayReservations.map((r) => {
            const expanded = expandedReservationId === r.id;
            const dateRangeLabel = formatStayDateRange(
              locale,
              r.check_in_date,
              r.check_out_date,
            );
            const nights = stayNightsCount(r.check_in_date, r.check_out_date);
            return (
              <li key={`r-${r.id}`}>
                <div className="card overflow-hidden">
                  <button
                    type="button"
                    className="card-hover flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
                    aria-expanded={expanded}
                    aria-label={expanded ? t("reservationCollapse") : t("reservationExpand")}
                    onClick={() =>
                      setExpandedReservationId((current) => (current === r.id ? null : r.id))
                    }
                  >
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <div className="flex min-w-0 items-center gap-2 font-semibold text-stay-navy">
                        <CountryFlag iso2={r.primary_guest_nationality_iso2} />
                        <span className="min-w-0 break-words">
                          {r.primary_guest_name || r.room_name}
                        </span>
                      </div>
                      <ReservationStayMeta
                        dateRangeLabel={dateRangeLabel}
                        nightsLabel={nights != null ? tc("nightsCount", { count: nights }) : null}
                      />
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
                      {reservationHasChannelBlock(r.id, blocks) ? (
                        <span className="badge bg-slate-200 text-slate-800">{t("channelBlocked")}</span>
                      ) : null}
                      <span className={`badge ${reservationStatusClass(r.status)}`}>
                        {statusLabel(r.status)}
                      </span>
                      <span className="text-muted" aria-hidden>
                        {expanded ? "▾" : "▸"}
                      </span>
                    </div>
                  </button>
                  {expanded ? (
                    <div className="border-t border-stay-border px-4 pb-4 pt-3">
                      <ReservationDetailPanel
                        reservationId={r.id}
                        embedded
                        onUpdated={onChanged}
                      />
                    </div>
                  ) : null}
                </div>
              </li>
            );
          })}

          {dayBlocks.map((b) => (
            <li key={`b-${b.id ?? b.block_ref}-${b.check_in}`}>
              <div className="card flex flex-wrap items-center justify-between gap-3 border-slate-300 bg-slate-50 px-4 py-3">
                <div>
                  <div className="font-semibold text-stay-navy">{t("blocked")}</div>
                  <div className="text-sm text-muted">
                    {b.check_in} → {b.check_out}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="badge bg-slate-200 text-slate-800">
                    {ts("stay")}
                  </span>
                  {b.can_unblock && b.id != null ? (
                    <button type="button" className="btn-ghost text-sm" onClick={() => void handleUnblock(b)}>
                      {t("unblock")}
                    </button>
                  ) : null}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {showChannelAri ? (
        <div className="card space-y-3 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-semibold text-stay-navy">{t("channelSyncTitle")}</h3>
            {onOpenBulkWizard ? (
              <button
                type="button"
                className="btn-ghost text-sm"
                onClick={() =>
                  onOpenBulkWizard({
                    roomId: room.id,
                    dateFrom: selection.date,
                    initialStep: 3,
                  })
                }
              >
                {t("editChannelPeriod")}
              </button>
            ) : null}
          </div>
          {!hasChannelData ? (
            <p className="text-sm text-muted">{t("channelNoData")}</p>
          ) : (
            <>
              {dayAvailability !== undefined ? (
                <p className="text-sm">
                  <span className="text-muted">{t("channelAvailability")}: </span>
                  <span
                    className={`font-semibold ${
                      dayAvailability === 0 ? "text-red-700" : "text-emerald-700"
                    }`}
                  >
                    {dayAvailability === 0 ? t("channelSoldOut") : dayAvailability}
                  </span>
                </p>
              ) : null}
              {dayRateRows.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[420px] text-sm">
                    <thead>
                      <tr className="border-b text-left text-muted">
                        <th className="py-2 pr-3 font-medium">{t("channelRatePlan")}</th>
                        <th className="py-2 pr-3 font-medium">{t("obpBaseRate")}</th>
                        <th className="py-2 pr-3 font-medium">{t("obpTiers")}</th>
                        <th className="py-2 pr-3 font-medium">{t("channelMinStay")}</th>
                        <th className="py-2 font-medium">{t("channelStopSell")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {dayRateRows.map((row) => (
                        <tr key={row.rate_plan_code} className="border-b border-slate-100">
                          <td className="py-2 pr-3">{row.rate_plan_title || row.rate_plan_code}</td>
                          <td className="py-2 pr-3 font-medium">
                            {formatChannelRateValue(row.rate, locale)} {row.currency}
                            {row.channex_push_rate ? (
                              <div className="text-xs font-normal text-muted">
                                {t("obpChannexPushNote", {
                                  rate: formatChannelRateValue(row.channex_push_rate, locale),
                                  occupancy: row.obp_primary_occupancy_adults ?? row.obp_anchor_adults ?? 2,
                                })}
                              </div>
                            ) : null}
                            {row.obp_normal_rate && row.channex_push_rate ? (
                              <div className="text-xs font-normal text-muted">
                                {t("obpNormalRate", {
                                  occupancy: row.obp_anchor_adults ?? row.obp_primary_occupancy_adults ?? 2,
                                })}
                                {": "}
                                {formatChannelRateValue(row.obp_normal_rate, locale)} {row.currency}
                              </div>
                            ) : null}
                          </td>
                          <td className="py-2 pr-3 text-xs text-stay-navy">
                            {row.obp_tiers?.length
                              ? formatObpTierList(row.obp_tiers, row.currency, locale)
                              : "—"}
                          </td>
                          <td className="py-2 pr-3">{row.min_stay_arrival}</td>
                          <td className="py-2">{row.stop_sell ? t("yes") : t("no")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}

      {showCalendarBlocks && canBlock ? (
        <div className="card space-y-3 p-4">
          <h3 className="font-semibold text-stay-navy">{t("blockUnitTitle")}</h3>
          <div className="flex flex-wrap gap-2">
            {freeUnits.length === 0 ? (
              <p className="text-sm text-muted">{t("noFreeUnits")}</p>
            ) : (
              freeUnits.map((u) => (
                <button
                  key={u.id}
                  type="button"
                  className={`rounded-full border px-3 py-1 text-sm ${
                    selectedUnitIds.has(u.id)
                      ? "border-stay-blue bg-stay-blue text-white"
                      : "border-stay-border bg-white text-stay-navy"
                  }`}
                  onClick={() => toggleUnit(u.id)}
                >
                  {u.code}
                </button>
              ))
            )}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-sm">
              <span className="label">{t("blockCheckIn")}</span>
              <input
                type="date"
                className="input mt-1"
                value={blockCheckIn}
                min={selection.date}
                onChange={(e) => {
                  const nextIn = e.target.value;
                  setBlockCheckIn(nextIn);
                  if (blockCheckOut <= nextIn) setBlockCheckOut(addDaysIso(nextIn, 1));
                }}
              />
            </label>
            <label className="block text-sm">
              <span className="label">{t("blockCheckOut")}</span>
              <input
                type="date"
                className="input mt-1"
                value={blockCheckOut}
                min={addDaysIso(blockCheckIn || selection.date, 1)}
                onChange={(e) => setBlockCheckOut(e.target.value)}
              />
            </label>
          </div>
          <button
            type="button"
            className="btn"
            disabled={selectedUnitIds.size === 0 || busy}
            onClick={() => void handleBlockSubmit()}
          >
            {t("blockSubmit")}
          </button>
        </div>
      ) : null}

      {actionError ? <p className="text-sm text-red-600">{actionError}</p> : null}
    </section>
  );
}
