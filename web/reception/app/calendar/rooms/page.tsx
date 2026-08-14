"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  ChannelBulkWizardModal,
  type BulkWizardPrefill,
  type ChannelRatePlanRow,
} from "@/app/_components/ChannelBulkWizardModal";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { RoomCalendarDayDetail } from "@/app/_components/RoomCalendarDayDetail";
import { RoomCalendarGrid } from "@/app/_components/RoomCalendarGrid";
import { RoomCalendarRangeControls } from "@/app/_components/RoomCalendarRangeControls";
import {
  historyMinStartIso,
  operationalFloorIso,
  ROLLING_WINDOW_DAYS,
} from "@/lib/calendarLayout";
import { formatDateRangeLabel } from "@/lib/locale-format";
import type {
  AppConfig,
  CalendarBlock,
  CalendarReservation,
  CalendarSelection,
  ChannelCalendarAri,
  ChannelRateDay,
  Room,
  SalesChannel,
} from "@/lib/types";
import { SALES_CHANNEL_STORAGE_KEY } from "@/lib/types";
import { normalizeChannelCalendarAri } from "@/lib/channelCalendarAri";
import { useSyncVersionsPoll } from "@/lib/useSyncVersionsPoll";
import { addDaysIso, todayIso } from "@/lib/utils";

function CalendarSkeleton() {
  return (
    <div className="card animate-pulse overflow-hidden p-4">
      <div className="mb-3 h-4 w-48 rounded bg-slate-200" />
      <div className="space-y-2">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-10 rounded bg-slate-100" />
        ))}
      </div>
    </div>
  );
}

export default function RoomCalendarPage() {
  const t = useTranslations("calendar");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [tenantName, setTenantName] = useState("");
  const [rooms, setRooms] = useState<Room[]>([]);
  const [rangeStart, setRangeStart] = useState(() => operationalFloorIso(todayIso()));
  const [historyEnabled, setHistoryEnabled] = useState(false);
  const [byRoom, setByRoom] = useState<Record<number, CalendarReservation[]>>({});
  const [blocks, setBlocks] = useState<CalendarBlock[]>([]);
  const [selection, setSelection] = useState<CalendarSelection | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCalendarBlocks, setShowCalendarBlocks] = useState(true);
  const [showChannelAri, setShowChannelAri] = useState(false);
  const [channelAvailabilityByUnitDate, setChannelAvailabilityByUnitDate] = useState<
    Record<number, Record<string, number>>
  >({});
  const [channelRatesByUnitDate, setChannelRatesByUnitDate] = useState<
    Record<number, Record<string, ChannelRateDay[]>>
  >({});
  const [ratePlans, setRatePlans] = useState<ChannelRatePlanRow[]>([]);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardPrefill, setWizardPrefill] = useState<BulkWizardPrefill>({});
  const [featureFlags, setFeatureFlags] = useState<AppConfig["feature_flags"]>();
  const [salesChannel, setSalesChannel] = useState<SalesChannel>("booking_com");

  useEffect(() => {
    try {
      const stored = localStorage.getItem(SALES_CHANNEL_STORAGE_KEY);
      if (stored === "direct" || stored === "booking_com") {
        setSalesChannel(stored);
      }
    } catch {
      // ignore
    }
  }, []);

  function handleSalesChannelChange(channel: SalesChannel) {
    setSalesChannel(channel);
    try {
      localStorage.setItem(SALES_CHANNEL_STORAGE_KEY, channel);
    } catch {
      // ignore
    }
  }

  const rangeEnd = addDaysIso(rangeStart, ROLLING_WINDOW_DAYS);
  const today = todayIso();
  const floor = operationalFloorIso(today);
  const historyMin = historyMinStartIso(floor);
  const rangeLabel = formatDateRangeLabel(locale, rangeStart, rangeEnd);

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
      let channelPanel = false;
      if (configRes.ok) {
        const config = (await configRes.json()) as AppConfig;
        setFeatureFlags(config.feature_flags);
        setShowCalendarBlocks(Boolean(config.feature_flags?.calendar_blocks));
        channelPanel = Boolean(config.feature_flags?.channel_panel);
        setShowChannelAri(channelPanel);
      } else {
        setShowChannelAri(false);
      }

      const roomsRes = await fetch("/api/stay/rooms/rooms/");
      if (!roomsRes.ok) throw new Error(t("loadRoomsFailed"));
      const roomList = (await roomsRes.json()) as Room[];
      setRooms(roomList);

      const to = rangeEnd;
      const channelQuery = `sales_channel=${encodeURIComponent(salesChannel)}`;
      const channelAriPromise = channelPanel
        ? fetch(`/api/stay/reception/calendar/channel-ari/?from=${rangeStart}&to=${to}&${channelQuery}`)
        : Promise.resolve(null);
      const ratePlansPromise = channelPanel
        ? fetch(`/api/stay/reception/channel/rate-plans/?${channelQuery}`)
        : Promise.resolve(null);

      const [blockRes, channelAriRes, ratePlansRes, ...roomResults] = await Promise.all([
        fetch(`/api/stay/reception/calendar/blocks/?from=${rangeStart}&to=${to}`),
        channelAriPromise,
        ratePlansPromise,
        ...roomList.map(async (room) => {
          const res = await fetch(
            `/api/stay/rooms/rooms/${room.id}/calendar/?from=${rangeStart}&to=${to}`,
          );
          if (!res.ok) throw new Error(t("loadRoomFailed", { code: room.code }));
          const data = (await res.json()) as CalendarReservation[];
          return [room.id, data] as const;
        }),
      ]);

      if (!blockRes.ok) throw new Error(t("loadBlocksFailed"));
      const blockList = (await blockRes.json()) as CalendarBlock[];
      setBlocks(blockList);
      setByRoom(Object.fromEntries(roomResults));

      if (channelAriRes?.ok) {
        const channelAri = normalizeChannelCalendarAri((await channelAriRes.json()) as ChannelCalendarAri);
        setChannelAvailabilityByUnitDate(channelAri.availabilityByUnitDate);
        setChannelRatesByUnitDate(channelAri.ratesByUnitDate);
      } else {
        setChannelAvailabilityByUnitDate({});
        setChannelRatesByUnitDate({});
      }

      if (ratePlansRes?.ok) {
        const data = (await ratePlansRes.json()) as { results?: ChannelRatePlanRow[] };
        setRatePlans(data.results ?? []);
      } else {
        setRatePlans([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      if (!background) {
        setInitialLoading(false);
      }
    }
  }, [rangeEnd, rangeStart, salesChannel, t, tc]);

  useEffect(() => {
    void load();
  }, [load]);

  useSyncVersionsPoll({
    onStale: () => {
      void load({ background: true });
    },
  });

  useEffect(() => {
    setSelection(null);
  }, [rangeStart]);

  const selectedRoom = useMemo(
    () => (selection ? rooms.find((r) => r.id === selection.roomId) ?? null : null),
    [rooms, selection],
  );

  const selectedReservations = selection ? byRoom[selection.roomId] || [] : [];

  function openBulkWizard(prefill: BulkWizardPrefill = {}) {
    setWizardPrefill(prefill);
    setWizardOpen(true);
  }

  return (
    <div>
      <ReceptionNav tenantName={tenantName} featureFlags={featureFlags} />
      <main className="mx-auto max-w-6xl space-y-4 px-4 py-6">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-bold">{t("title")}</h1>
          <RoomCalendarRangeControls
            rangeStart={rangeStart}
            rangeLabel={rangeLabel}
            floor={floor}
            historyMin={historyMin}
            historyEnabled={historyEnabled}
            onRangeStartChange={setRangeStart}
            onHistoryEnabledChange={setHistoryEnabled}
          />
          <button type="button" className="btn" onClick={() => void load()}>
            {tc("refresh")}
          </button>
          {showChannelAri ? (
            <div className="flex flex-wrap items-center gap-1 rounded-lg border border-stay-border bg-white p-0.5">
              <button
                type="button"
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  salesChannel === "direct"
                    ? "bg-stay-blue text-white"
                    : "text-stay-muted hover:text-stay-navy"
                }`}
                onClick={() => handleSalesChannelChange("direct")}
              >
                {t("pricingChannelDirect")}
              </button>
              <button
                type="button"
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  salesChannel === "booking_com"
                    ? "bg-stay-blue text-white"
                    : "text-stay-muted hover:text-stay-navy"
                }`}
                onClick={() => handleSalesChannelChange("booking_com")}
              >
                {t("pricingChannelBookingCom")}
              </button>
            </div>
          ) : null}
          {showChannelAri ? (
            <button type="button" className="btn" onClick={() => openBulkWizard()}>
              {t("bulkUpdateButton")}
            </button>
          ) : null}
        </div>

        {error ? <p className="text-sm text-red-600">{error}</p> : null}

        {initialLoading ? <CalendarSkeleton /> : null}

        {!initialLoading ? (
          <>
            <RoomCalendarGrid
              rangeStart={rangeStart}
              rooms={rooms}
              byRoom={byRoom}
              blocks={blocks}
              selection={selection}
              onSelect={setSelection}
              channelAvailability={showChannelAri ? channelAvailabilityByUnitDate : undefined}
              channelRates={showChannelAri ? channelRatesByUnitDate : undefined}
              showObpTiers={salesChannel === "booking_com"}
            />
            <RoomCalendarDayDetail
              selection={selection}
              room={selectedRoom}
              rooms={rooms}
              reservations={selectedReservations}
              byRoom={byRoom}
              blocks={blocks}
              onChanged={() => void load({ background: true })}
              showCalendarBlocks={showCalendarBlocks}
              showChannelAri={showChannelAri}
              channelAvailability={channelAvailabilityByUnitDate}
              channelRates={channelRatesByUnitDate}
              onOpenBulkWizard={showChannelAri ? openBulkWizard : undefined}
            />
          </>
        ) : null}
      </main>
      {showChannelAri ? (
        <ChannelBulkWizardModal
          open={wizardOpen}
          onClose={() => setWizardOpen(false)}
          onApplied={() => void load({ background: true })}
          rooms={rooms}
          ratePlans={ratePlans}
          initialUnitId={wizardPrefill.roomId}
          initialDateFrom={wizardPrefill.dateFrom}
          initialStep={wizardPrefill.initialStep}
          salesChannel={salesChannel}
          channelRatesByUnitDate={channelRatesByUnitDate}
        />
      ) : null}
    </div>
  );
}
