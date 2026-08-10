import { describe, expect, it } from "vitest";
import {
  timelineEvisitorFailedGuests,
  timelineEvisitorLabel,
  timelineEvisitorTone,
} from "@/lib/evisitorTimelineBadge";
import type { GuestLite } from "@/lib/types";

function guest(partial: Partial<GuestLite> & Pick<GuestLite, "id" | "evisitor_status">): GuestLite {
  return {
    first_name: "Test",
    last_name: "Guest",
    email: "",
    phone: "",
    date_of_birth: null,
    sex: "",
    address: "",
    is_primary: false,
    nationality: "",
    document_number: "",
    document_type: "",
    date_of_issue: null,
    date_of_expiry: null,
    issuing_authority: "",
    personal_id_number: "",
    evisitor_error: "",
    face_photo_url: "",
    evisitor_required: true,
    ...partial,
  };
}

describe("timelineEvisitorTone", () => {
  it("returns ok for complete", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "complete",
        evisitor_progress: { required: 3, sent: 3, failed: 0, pending: 0 },
      }),
    ).toBe("ok");
  });

  it("returns ok for checked_out", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "checked_out",
        evisitor_progress: { required: 2, sent: 2, failed: 0, pending: 0 },
      }),
    ).toBe("ok");
  });

  it("returns pending for incomplete with failed=0", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "incomplete",
        evisitor_progress: { required: 3, sent: 1, failed: 0, pending: 2 },
      }),
    ).toBe("pending");
  });

  it("returns error when incomplete and failed>0 even with pending", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "incomplete",
        evisitor_progress: { required: 3, sent: 1, failed: 1, pending: 1 },
      }),
    ).toBe("error");
  });

  it("returns null when not checked_in", () => {
    expect(
      timelineEvisitorTone({
        status: "expected",
        evisitor_summary: "incomplete",
        evisitor_progress: { required: 2, sent: 0, failed: 0, pending: 2 },
      }),
    ).toBeNull();
  });

  it("returns null when summary is none", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "none",
        evisitor_progress: { required: 0, sent: 0, failed: 0, pending: 0 },
      }),
    ).toBeNull();
  });

  it("returns null when required is 0", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "complete",
        evisitor_progress: { required: 0, sent: 0, failed: 0, pending: 0 },
      }),
    ).toBeNull();
  });

  it("returns null when progress is missing", () => {
    expect(
      timelineEvisitorTone({
        status: "checked_in",
        evisitor_summary: "incomplete",
      }),
    ).toBeNull();
  });
});

describe("timelineEvisitorLabel", () => {
  it("formats sent/required", () => {
    expect(timelineEvisitorLabel({ required: 3, sent: 1, failed: 1, pending: 1 })).toBe(
      "eVisitor 1/3",
    );
  });
});

describe("timelineEvisitorFailedGuests", () => {
  it("includes failed and checkout_failed required guests", () => {
    const guests = [
      guest({ id: 1, evisitor_status: "failed", evisitor_error: "bad doc" }),
      guest({ id: 2, evisitor_status: "checkout_failed", evisitor_error: "timeout" }),
      guest({ id: 3, evisitor_status: "sent" }),
      guest({ id: 4, evisitor_status: "failed", evisitor_required: false }),
      guest({ id: 5, evisitor_status: "not_sent" }),
    ];
    expect(timelineEvisitorFailedGuests(guests).map((g) => g.id)).toEqual([1, 2]);
  });

  it("returns empty for undefined guests", () => {
    expect(timelineEvisitorFailedGuests(undefined)).toEqual([]);
  });
});
