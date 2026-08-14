import { overlapsDay } from "@/lib/calendarLayout";
import type { CalendarBlock, CalendarReservation, Room } from "@/lib/types";
import { addDaysIso, todayIso } from "@/lib/utils";

export function isUnitBusyOnNight(
  unitId: number,
  nightIso: string,
  reservations: CalendarReservation[],
  blocks: CalendarBlock[],
): boolean {
  for (const reservation of reservations) {
    if (overlapsDay(reservation.check_in_date, reservation.check_out_date, nightIso)) {
      return true;
    }
  }
  for (const block of blocks) {
    if (block.unit_id === unitId && overlapsDay(block.check_in, block.check_out, nightIso)) {
      return true;
    }
  }
  return false;
}

export function freeUnitsForNight(
  rooms: Room[],
  nightIso: string,
  byRoom: Record<number, CalendarReservation[]>,
  blocks: CalendarBlock[],
): Room[] {
  return rooms.filter((room) => {
    const reservations = byRoom[room.id] || [];
    return !isUnitBusyOnNight(room.id, nightIso, reservations, blocks);
  });
}

export function isUnitFreeForRange(
  unitId: number,
  checkIn: string,
  checkOut: string,
  byRoom: Record<number, CalendarReservation[]>,
  blocks: CalendarBlock[],
): boolean {
  if (checkOut <= checkIn) return false;
  let night = checkIn;
  while (night < checkOut) {
    const reservations = byRoom[unitId] || [];
    if (isUnitBusyOnNight(unitId, night, reservations, blocks)) {
      return false;
    }
    night = addDaysIso(night, 1);
  }
  return true;
}

export function isDayTappable(dateIso: string, today: string = todayIso()): boolean {
  return dateIso >= today;
}
