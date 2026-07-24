"use client";

import { useTranslations } from "next-intl";
import type { ReceptionPropertySummary } from "@/lib/propertySettings";

type Props = {
  properties: ReceptionPropertySummary[];
  propertyId: number | null;
  onChange: (propertyId: number) => void;
  disabled?: boolean;
};

export function SettingsPropertyPicker({ properties, propertyId, onChange, disabled }: Props) {
  const t = useTranslations("settings");

  if (properties.length === 0) {
    return <p className="text-sm text-stay-muted">{t("noProperties")}</p>;
  }

  if (properties.length === 1) {
    return (
      <p className="text-sm text-stay-navy">
        <span className="font-medium">{t("property")}:</span> {properties[0].name}
      </p>
    );
  }

  return (
    <label className="flex flex-wrap items-center gap-2 text-sm text-stay-navy">
      <span className="font-medium">{t("property")}</span>
      <select
        className="rounded-lg border border-stay-border bg-white px-3 py-1.5"
        value={propertyId ?? ""}
        disabled={disabled}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next)) onChange(next);
        }}
      >
        <option value="" disabled>
          {t("propertyPlaceholder")}
        </option>
        {properties.map((property) => (
          <option key={property.id} value={property.id}>
            {property.name}
          </option>
        ))}
      </select>
    </label>
  );
}
