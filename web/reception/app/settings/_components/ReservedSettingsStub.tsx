"use client";

import { useTranslations } from "next-intl";
import { SettingsStubPanel } from "@/app/settings/_components/SettingsStubPanel";

type Props = {
  sectionKey:
    | "security"
    | "integrations"
    | "branding"
    | "localization"
    | "payments"
    | "reviews"
    | "users";
};

export function ReservedSettingsStub({ sectionKey }: Props) {
  const t = useTranslations(`settings.reserved.${sectionKey}`);
  const tr = useTranslations("settings.reserved");
  return <SettingsStubPanel title={t("title")} message={tr("body")} />;
}
