"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useSettingsShell } from "@/app/settings/_components/SettingsShell";
import { firstEnabledSettingsTab, settingsTabHref } from "@/lib/propertySettings";

export default function SettingsIndexPage() {
  const router = useRouter();
  const { root, propertyId, loading } = useSettingsShell();

  useEffect(() => {
    if (loading || !root) return;
    const tab = firstEnabledSettingsTab(root.tabs);
    if (!tab) return;
    router.replace(settingsTabHref(tab, propertyId));
  }, [loading, root, propertyId, router]);

  return null;
}
