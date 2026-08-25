import type { ChannexReview } from "@/lib/types";

type Badge = {
  key: string;
  className: string;
};

const MUTED_BADGE = "rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700";

export function reviewStatusBadges(review: ChannexReview): Badge[] {
  const badges: Badge[] = [];

  if (review.reply_pending_moderation) {
    badges.push({
      key: "replyPendingModeration",
      className: "rounded bg-sky-100 px-1.5 py-0.5 text-xs text-sky-900",
    });
  } else if (review.reply_published || (review.is_replied && review.reply_sent_at)) {
    badges.push({
      key: "replyPublished",
      className: "rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-900",
    });
  } else if (!review.is_replied && review.can_reply) {
    badges.push({
      key: "needsReply",
      className: "rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-900",
    });
  } else if (review.reply_blocked_reason === "rating_only") {
    badges.push({
      key: "notRespondable",
      className: MUTED_BADGE,
    });
  } else if (review.reply_blocked_reason === "expired") {
    badges.push({
      key: "replyExpiredBadge",
      className: MUTED_BADGE,
    });
  }

  return badges;
}
