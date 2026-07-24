"use client";

import { useTranslations } from "next-intl";

type Props = {
  title?: string;
  message?: string;
};

export function SettingsStubPanel({ title, message }: Props) {
  const t = useTranslations("settings");
  return (
    <section className="rounded-xl border border-dashed border-stay-border bg-white px-4 py-8 text-center">
      <h2 className="text-lg font-semibold text-stay-navy">{title ?? t("stubTitle")}</h2>
      <p className="mt-2 text-sm text-stay-muted">{message ?? t("stubBody")}</p>
    </section>
  );
}
