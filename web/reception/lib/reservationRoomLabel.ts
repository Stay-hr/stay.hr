/**
 * Timeline room line: single unit code + base name when room_name ends with exact ` - {code}`.
 * No fuzzy / mid-string parsing.
 */
export function formatReservationRoomLine(roomName: string, roomCodes?: string[]): string {
  const name = roomName ?? "";
  const codes = (roomCodes ?? []).filter(Boolean);
  if (codes.length !== 1) return name;

  const code = codes[0];
  const suffix = ` - ${code}`;
  if (!name.endsWith(suffix)) return name;

  const base = name.slice(0, -suffix.length).trim();
  if (!base) return name;

  return `${code} · ${base}`;
}
