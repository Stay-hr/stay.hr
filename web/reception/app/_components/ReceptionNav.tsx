"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { SessionLocaleSync } from "@/app/_components/SessionLocaleSync";
import { StayLogo } from "@/app/_components/StayLogo";
import {
  settingsSurfaceEnabled,
  type PropertySettingsRoot,
} from "@/lib/propertySettings";
import {
  buildNeedsReplyBadgeUrl,
  formatNeedsReplyBadgeCount,
} from "@/lib/messageInbox";
import type { AppConfig, MessageThreadsListResponse } from "@/lib/types";

type Props = {
  tenantName?: string;
  featureFlags?: AppConfig["feature_flags"];
};

export function ReceptionNav({ tenantName, featureFlags: featureFlagsProp }: Props) {
  const pathname = usePathname();
  const router = useRouter();
  const t = useTranslations("nav");
  const [featureFlags, setFeatureFlags] = useState(featureFlagsProp);
  const [channelManager, setChannelManager] = useState<string | undefined>();
  const [settingsEnabled, setSettingsEnabled] = useState(false);
  const [needsReplyBadge, setNeedsReplyBadge] = useState<string | null>(null);

  useEffect(() => {
    if (featureFlagsProp) {
      setFeatureFlags(featureFlagsProp);
    }
    void fetch("/api/stay/app/config")
      .then((res) => (res.ok ? res.json() : null))
      .then((config: AppConfig | null) => {
        if (config?.feature_flags) setFeatureFlags(config.feature_flags);
        if (config?.channel_manager) setChannelManager(config.channel_manager);
      })
      .catch(() => undefined);

    void fetch("/api/stay/reception/settings/")
      .then((res) => (res.ok ? res.json() : null))
      .then((root: PropertySettingsRoot | null) => {
        setSettingsEnabled(settingsSurfaceEnabled(root));
      })
      .catch(() => setSettingsEnabled(false));
  }, [featureFlagsProp]);

  useEffect(() => {
    let cancelled = false;
    void fetch(buildNeedsReplyBadgeUrl())
      .then((res) => (res.ok ? res.json() : null))
      .then((data: MessageThreadsListResponse | null) => {
        if (cancelled || !data) return;
        setNeedsReplyBadge(formatNeedsReplyBadgeCount(data.needs_reply_count));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
    router.refresh();
  }

  const linkClass = (href: string) =>
    `rounded-xl px-3 py-2 text-sm font-medium transition ${
      pathname === href ||
      (href.startsWith("/reports") && pathname.startsWith("/reports")) ||
      (href.startsWith("/settings") && pathname.startsWith("/settings"))
        ? "bg-stay-blue text-white shadow-sm"
        : "text-stay-muted hover:bg-stay-blue-light hover:text-stay-blue"
    }`;

  const whatsappLinkClass = pathname.startsWith("/whatsapp")
    ? "rounded-xl bg-stay-blue px-3 py-2 text-sm font-medium text-white shadow-sm"
    : "rounded-xl px-3 py-2 text-sm font-medium text-stay-muted transition hover:bg-stay-blue-light hover:text-stay-blue";

  const onTimeline = pathname === "/";

  return (
    <header className="border-b border-stay-border bg-white shadow-sm">
      <SessionLocaleSync />
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div className="flex min-w-0 flex-col gap-1">
          <StayLogo href="/" subtitle={t("reception")} />
          {tenantName ? (
            <div className="truncate pl-0.5 text-sm font-semibold text-stay-navy">{tenantName}</div>
          ) : null}
        </div>
        <nav className="flex flex-wrap items-center gap-1">
          <Link href={onTimeline ? "/calendar/rooms" : "/"} className="btn-ghost">
            {onTimeline ? t("calendar") : t("timeline")}
          </Link>
          {featureFlags?.reception_create_reservation ? (
            <Link href="/reservations/new" className={linkClass("/reservations/new")}>
              {t("newReservation")}
            </Link>
          ) : null}
          {featureFlags?.reception_booking_intake ? (
            <Link href="/booking-intake" className={linkClass("/booking-intake")}>
              {t("bookingIntake")}
            </Link>
          ) : null}
          {channelManager === "channex" ? (
            <Link href="/reviews" className={linkClass("/reviews")}>
              {t("reviews")}
            </Link>
          ) : null}
          <Link href="/messages" className={linkClass("/messages")}>
            <span className="inline-flex items-center gap-1.5">
              {t("messages")}
              {needsReplyBadge ? (
                <span className="inline-flex min-w-5 items-center justify-center rounded-full bg-red-600 px-1.5 text-[10px] font-semibold leading-4 text-white">
                  {needsReplyBadge}
                </span>
              ) : null}
            </span>
          </Link>
          <Link href="/reports/property-financial" className={linkClass("/reports/property-financial")}>
            {t("reports")}
          </Link>
          {settingsEnabled ? (
            <Link href="/settings" className={linkClass("/settings")}>
              {t("settings")}
            </Link>
          ) : null}
          <Link href="/whatsapp/overview" className={whatsappLinkClass}>
            {t("whatsapp")}
          </Link>
          <button type="button" onClick={logout} className="btn-ghost ml-2">
            {t("logout")}
          </button>
        </nav>
      </div>
    </header>
  );
}
