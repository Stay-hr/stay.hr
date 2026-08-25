"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { ReviewContentText } from "@/app/_components/ReviewContentText";
import type { AppConfig, ChannexReview, ChannexReviewsListResponse } from "@/lib/types";
import { reviewStatusBadges } from "@/lib/review-status-badges";

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

export default function ReviewsPage() {
  const t = useTranslations("guestReviews");
  const tc = useTranslations("common");
  const router = useRouter();
  const [tenantName, setTenantName] = useState("");
  const [reviews, setReviews] = useState<ChannexReview[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [unrepliedOnly, setUnrepliedOnly] = useState(true);
  const [displayLang, setDisplayLang] = useState("hr");

  useEffect(() => {
    void fetch("/api/stay/app/config")
      .then((res) => (res.ok ? res.json() : null))
      .then((config: AppConfig | null) => {
        if (config?.tenant?.name) setTenantName(config.tenant.name);
      })
      .catch(() => undefined);
  }, []);

  const loadReviews = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        sync: "auto",
        page: "1",
        page_size: "50",
        lang: displayLang,
        translate: "1",
      });
      if (unrepliedOnly) params.set("unreplied", "1");
      const res = await fetch(`/api/stay/reception/reviews/?${params.toString()}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || t("loadFailed"));
      }
      const data = (await res.json()) as ChannexReviewsListResponse;
      setReviews(data.reviews ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [displayLang, t, unrepliedOnly]);

  useEffect(() => {
    void loadReviews();
  }, [loadReviews]);

  return (
    <div className="min-h-screen">
      <ReceptionNav tenantName={tenantName} />
      <main className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-xl font-semibold text-stay-navy">{t("inboxTitle")}</h1>
          <div className="flex flex-wrap items-center gap-2">
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
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={unrepliedOnly}
                onChange={(event) => setUnrepliedOnly(event.target.checked)}
              />
              {t("filterUnreplied")}
            </label>
            <button type="button" className="btn-ghost btn-sm" onClick={() => void loadReviews()} disabled={loading}>
              {tc("refresh")}
            </button>
          </div>
        </div>

        {error ? <p className="text-sm text-red-600">{error}</p> : null}

        <div className="space-y-2">
          {loading && reviews.length === 0 ? (
            <p className="text-sm text-muted">{tc("loading")}</p>
          ) : reviews.length === 0 ? (
            <p className="text-sm text-muted">{t("inboxEmpty")}</p>
          ) : (
            reviews.map((review) => (
              <button
                key={review.id}
                type="button"
                disabled={!review.reservation_id}
                onClick={() => {
                  if (review.reservation_id) {
                    router.push(`/reservations/${review.reservation_id}/reviews/${review.id}`);
                  }
                }}
                className={`w-full rounded-lg border p-3 text-left transition ${
                  review.reservation_id ? "bg-white hover:bg-slate-50" : "cursor-not-allowed bg-slate-50 opacity-70"
                }`}
              >
                {review.reservation_id ? (
                  <p className="text-sm font-semibold text-stay-blue">
                    {t("linkedReservation", {
                      code: review.booking_code || review.reservation_id,
                    })}
                  </p>
                ) : null}
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{otaLabel(review.ota)}</span>
                  {review.overall_score != null ? <span>{review.overall_score}/10</span> : null}
                  {reviewStatusBadges(review).map((badge) => (
                    <span key={badge.key} className={badge.className}>
                      {t(badge.key)}
                    </span>
                  ))}
                </div>
                <div className="mt-1">
                  <ReviewContentText review={review} className="line-clamp-2 text-sm text-muted" />
                </div>
                <p className="mt-1 text-xs text-muted">{formatReviewTime(review.received_at)}</p>
              </button>
            ))
          )}
        </div>
      </main>
    </div>
  );
}
