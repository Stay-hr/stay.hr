import { describe, expect, it } from "vitest";
import { formatReservationRoomLine } from "@/lib/reservationRoomLabel";

describe("formatReservationRoomLine", () => {
  it("reformats exact trailing ` - {code}` for a single code", () => {
    expect(formatReservationRoomLine("Luxury Room Uzorita - R2", ["R2"])).toBe(
      "R2 · Luxury Room Uzorita",
    );
  });

  it("does not fuzzy-match mid-string codes", () => {
    expect(formatReservationRoomLine("Room R2 Deluxe", ["R2"])).toBe("Room R2 Deluxe");
  });

  it("falls back to original name when base would be empty", () => {
    expect(formatReservationRoomLine(" - R2", ["R2"])).toBe(" - R2");
  });

  it("returns room_name for multi-code or missing codes", () => {
    expect(
      formatReservationRoomLine("Luxury Room Uzorita - R1, Luxury Room Uzorita - R2", [
        "R1",
        "R2",
      ]),
    ).toBe("Luxury Room Uzorita - R1, Luxury Room Uzorita - R2");
    expect(formatReservationRoomLine("Luxury Room Uzorita - R2")).toBe(
      "Luxury Room Uzorita - R2",
    );
    expect(formatReservationRoomLine("Luxury Room Uzorita - R2", [])).toBe(
      "Luxury Room Uzorita - R2",
    );
  });
});
