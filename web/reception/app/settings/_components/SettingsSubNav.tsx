"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  SETTINGS_TAB_ORDER,
  settingsTabHref,
  type SettingsTabKey,
  type SettingsTabs,
} from "@/lib/propertySettings";

type Props = {
  tabs: SettingsTabs;
  propertyId: number | null;
};

export function SettingsSubNav({ tabs, propertyId }: Props) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const t = useTranslations("settings.nav");

  const labels: Record<SettingsTabKey, string> = {
    general: t("general"),
    guest: t("guest"),
    checkin: t("checkin"),
    automation: t("automation"),
  };

  const enabled = SETTINGS_TAB_ORDER.filter((key) => tabs[key]);
  if (enabled.length === 0) return null;

  const propertyQuery = searchParams.get("property");
  const resolvedPropertyId =
    propertyId ??
    (propertyQuery && Number.isFinite(Number(propertyQuery)) ? Number(propertyQuery) : null);

  return (
    <nav className="mb-4 flex flex-wrap gap-2 border-b border-stay-border pb-3" aria-label={t("title")}>
      {enabled.map((key) => {
        const href = settingsTabHref(key, resolvedPropertyId);
        const active = pathname === `/settings/${key}` || pathname.startsWith(`/settings/${key}/`);
        return (
          <Link
            key={key}
            href={href}
            className={`rounded-lg px-3 py-1.5 text-sm font-medium transition ${
              active
                ? "bg-stay-blue text-white"
                : "text-stay-muted hover:bg-stay-blue-light hover:text-stay-blue"
            }`}
          >
            {labels[key]}
          </Link>
        );
      })}
    </nav>
  );
}
