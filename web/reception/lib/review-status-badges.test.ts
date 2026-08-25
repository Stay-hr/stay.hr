import { describe, expect, it } from "vitest";
import { reviewStatusBadges } from "@/lib/review-status-badges";
import type { ChannexReview } from "@/lib/types";

function review(overrides: Partial<ChannexReview> = {}): ChannexReview {
  return {
    id: 1,
    channex_review_id: "r1",
    reservation_id: 10,
    booking_code: "123",
    ota: "BookingCom",
    guest_name: "Guest",
    overall_score: 8,
    scores: [],
    tags: [],
    content: "Nice stay",
    reply: null,
    is_replied: false,
    is_hidden: false,
    expired_at: null,
    received_at: null,
    reply_sent_at: null,
    can_reply: true,
    can_submit_guest_review: false,
    ...overrides,
  };
}

describe("reviewStatusBadges", () => {
  it("keeps published as the dominant status over rating_only", () => {
    const badges = reviewStatusBadges(
      review({
        is_replied: true,
        reply: "Thanks",
        reply_sent_at: "2026-08-20T10:00:00Z",
        reply_published: true,
        can_reply: false,
        reply_blocked_reason: "replied",
        content: "",
      }),
    );
    expect(badges.map((badge) => badge.key)).toEqual(["replyPublished"]);
  });

  it("shows notRespondable only for rating-only reviews", () => {
    const badges = reviewStatusBadges(
      review({
        content: "",
        can_reply: false,
        reply_blocked_reason: "rating_only",
      }),
    );
    expect(badges.map((badge) => badge.key)).toEqual(["notRespondable"]);
  });

  it("shows the expired badge when the reply window closed", () => {
    const badges = reviewStatusBadges(
      review({
        can_reply: false,
        reply_blocked_reason: "expired",
        expired_at: "2026-08-24T13:35:53Z",
      }),
    );
    expect(badges.map((badge) => badge.key)).toEqual(["replyExpiredBadge"]);
  });
});
