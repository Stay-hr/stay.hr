"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { ReviewContentText } from "@/app/_components/ReviewContentText";
import { extractApiError } from "@/lib/api-error";
import type { ChannexReview } from "@/lib/types";
import { reviewStatusBadges } from "@/lib/review-status-badges";

function blockedReplyMessage(
  review: ChannexReview,
  t: (key: string, values?: Record<string, string>) => string,
  formatDate: (iso: string | null) => string,
): string {
  switch (review.reply_blocked_reason) {
    case "rating_only":
      return t("replyBlockedRatingOnly");
    case "expired":
      return t("replyBlockedExpired", { date: formatDate(review.expired_at) });
    case "replied":
      return t("replyBlockedReplied");
    case "airbnb_hidden":
      return t("replyBlockedAirbnbHidden");
    default:
      return t("replyBlockedReplied");
  }
}

const REVIEW_LANGS = ["hr", "en", "de", "es", "fr", "it"] as const;

function formatReviewTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function otaLabel(ota: string): string {
  if (ota === "BookingCom") return "Booking.com";
  if (ota === "AirBNB") return "Airbnb";
  if (ota === "Expedia") return "Expedia";
  return ota || "OTA";
}

export default function ReservationReviewDetailPage() {
  const params = useParams<{ id: string; reviewId: string }>();
  const t = useTranslations("guestReviews");
  const tc = useTranslations("common");
  const reservationId = Number(params.id);
  const reviewId = Number(params.reviewId);
  const [review, setReview] = useState<ChannexReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [displayLang, setDisplayLang] = useState("hr");
  const [replyExpanded, setReplyExpanded] = useState(false);
  const [hint, setHint] = useState("");
  const [replyText, setReplyText] = useState("");
  const [busy, setBusy] = useState(false);
  const [composing, setComposing] = useState(false);
  const [actionMessage, setActionMessage] = useState("");

  const loadReview = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({ lang: displayLang, translate: "1", sync: "0" });
      const res = await fetch(`/api/stay/reception/reviews/${reviewId}/?${params.toString()}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || t("loadFailed"));
      }
      setReview((await res.json()) as ChannexReview);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [displayLang, reviewId, t]);

  useEffect(() => {
    void loadReview();
  }, [loadReview]);

  async function handleCompose() {
    if (!review) return;
    setComposing(true);
    setActionMessage("");
    try {
      const res = await fetch(`/api/stay/reception/reviews/${review.id}/compose-reply/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          hint,
          language: review.suggested_reply_language || undefined,
        }),
      });
      if (!res.ok) {
        throw new Error(await extractApiError(res, t("composeFailed")));
      }
      const data = (await res.json()) as { body_text: string };
      setReplyText(data.body_text);
      setActionMessage(t("composeReady"));
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : t("composeFailed"));
    } finally {
      setComposing(false);
    }
  }

  async function handleReply() {
    if (!review) return;
    const text = replyText.trim();
    if (!text) return;
    setBusy(true);
    setActionMessage("");
    try {
      const res = await fetch(`/api/stay/reception/reviews/${review.id}/reply/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reply: text }),
      });
      if (!res.ok) {
        throw new Error(await extractApiError(res, t("replyFailed")));
      }
      const successKey =
        review.ota === "BookingCom" ? "replySuccessBooking" : "replySuccess";
      setActionMessage(t(successKey));
      setReplyExpanded(false);
      setReplyText("");
      setHint("");
      await loadReview();
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : t("replyFailed"));
    } finally {
      setBusy(false);
    }
  }

  const title =
    review?.booking_code != null && review.booking_code !== ""
      ? t("linkedReservation", { code: review.booking_code })
      : t("title");

  return (
    <div className="min-h-screen">
      <ReceptionNav tenantName="" />
      <main className="mx-auto max-w-2xl space-y-4 px-4 py-6">
        <div className="flex items-center gap-2 text-sm">
          <Link href={`/reservations/${reservationId}`} className="text-stay-blue hover:underline">
            ← {title}
          </Link>
        </div>

        {loading ? <p className="text-sm text-muted">{tc("loading")}</p> : null}
        {error ? <p className="text-sm text-red-600">{error}</p> : null}

        {review ? (
          <div className="space-y-4 rounded-lg border bg-white p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-stay-navy">{otaLabel(review.ota)}</span>
                {review.overall_score != null ? (
                  <span className="text-lg font-semibold">{review.overall_score}/10</span>
                ) : null}
                {reviewStatusBadges(review).map((badge) => (
                  <span key={badge.key} className={badge.className}>
                    {t(badge.key)}
                  </span>
                ))}
              </div>
              <label className="flex items-center gap-2 text-sm">
                <span>{t("displayLanguage")}</span>
                <select
                  className="input input-sm"
                  value={displayLang}
                  onChange={(event) => setDisplayLang(event.target.value)}
                >
                  {REVIEW_LANGS.map((code) => (
                    <option key={code} value={code}>
                      {code.toUpperCase()}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {review.guest_name ? <p className="text-sm">{review.guest_name}</p> : null}
            <ReviewContentText review={review} />
            {review.received_at ? (
              <p className="text-xs text-muted">{formatReviewTime(review.received_at)}</p>
            ) : null}

            {review.reply ? (
              <div className="rounded-md border-l-4 border-stay-blue bg-stay-surface/40 p-2 text-sm">
                <p className="text-xs font-medium text-muted">{t("yourReply")}</p>
                <p className="whitespace-pre-wrap">{review.reply}</p>
                {review.reply_pending_moderation ? (
                  <p className="mt-1 text-xs text-sky-800">{t("replyPendingModeration")}</p>
                ) : null}
              </div>
            ) : null}

            {review.ota === "BookingCom" && (review.can_reply || review.reply_pending_moderation) ? (
              <p className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
                {t("bookingModerationInfo")}
              </p>
            ) : null}

            {review.can_submit_guest_review ? (
              <p className="text-sm text-amber-800">{t("airbnbGuestReviewHint")}</p>
            ) : null}

            {review.can_reply ? (
              <div className="space-y-2">
                {review.expired_at ? (
                  <p className="text-xs text-muted">
                    {t("replyDeadline", { date: formatReviewTime(review.expired_at) })}
                  </p>
                ) : null}
                {!replyExpanded ? (
                  <button type="button" className="btn btn-sm" onClick={() => setReplyExpanded(true)}>
                    {t("replyStartAction")}
                  </button>
                ) : (
                  <>
                    <textarea
                      className="input min-h-[60px] w-full text-sm"
                      value={hint}
                      onChange={(event) => setHint(event.target.value)}
                      placeholder={t("composeHintOptional")}
                      disabled={busy || composing}
                    />
                    <button
                      type="button"
                      className="btn-ghost btn-sm"
                      onClick={() => void handleCompose()}
                      disabled={busy || composing}
                    >
                      {composing ? tc("loading") : t("generateAction")}
                    </button>
                    <textarea
                      className="input min-h-[120px] w-full text-sm"
                      value={replyText}
                      onChange={(event) => setReplyText(event.target.value)}
                      placeholder={t("replyPlaceholder")}
                      disabled={busy}
                    />
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="btn-ghost btn-sm"
                        onClick={() => {
                          setReplyExpanded(false);
                          setReplyText("");
                          setHint("");
                        }}
                        disabled={busy}
                      >
                        {t("replyCancel")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => void handleReply()}
                        disabled={busy || !replyText.trim()}
                      >
                        {busy ? tc("loading") : t("replyAction")}
                      </button>
                    </div>
                  </>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted">
                {blockedReplyMessage(review, t, formatReviewTime)}
              </p>
            )}

            {actionMessage ? <p className="text-sm text-muted">{actionMessage}</p> : null}
          </div>
        ) : null}
      </main>
    </div>
  );
}
