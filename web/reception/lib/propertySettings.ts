export type SettingsCapabilities = {
  guest_settings: boolean;
  preview: boolean;
  share: boolean;
  automation: boolean;
  checkin: boolean;
  general: boolean;
};

export type SettingsTabs = {
  general: boolean;
  guest: boolean;
  checkin: boolean;
  automation: boolean;
};

export type PropertySettingsRoot = {
  capabilities: SettingsCapabilities;
  tabs: SettingsTabs;
};

export type ReceptionPropertySummary = {
  id: number;
  name: string;
  slug: string;
};

export const SETTINGS_TAB_ORDER = ["guest", "general", "checkin", "automation"] as const;
export type SettingsTabKey = (typeof SETTINGS_TAB_ORDER)[number];

export const RESERVED_SETTINGS_SECTIONS = [
  "security",
  "integrations",
  "branding",
  "localization",
  "payments",
  "reviews",
  "users",
] as const;
export type ReservedSettingsSection = (typeof RESERVED_SETTINGS_SECTIONS)[number];

export const PROPERTY_SETTINGS_STORAGE_KEY = "stay.reception.settings.propertyId";

export function settingsSurfaceEnabled(root: PropertySettingsRoot | null | undefined): boolean {
  if (!root) return false;
  const tabs = Object.values(root.tabs ?? {});
  const caps = Object.values(root.capabilities ?? {});
  return tabs.some(Boolean) || caps.some(Boolean);
}

export function firstEnabledSettingsTab(tabs: SettingsTabs | null | undefined): SettingsTabKey | null {
  if (!tabs) return null;
  for (const key of SETTINGS_TAB_ORDER) {
    if (tabs[key]) return key;
  }
  return null;
}

export function settingsTabHref(tab: SettingsTabKey, propertyId?: number | null): string {
  const base = `/settings/${tab}`;
  if (propertyId == null || !Number.isFinite(propertyId)) return base;
  return `${base}?property=${propertyId}`;
}
