export type GeneralSettingsDto = {
  settings_version: number;
  name: string;
  slug: string;
  address: string;
  timezone: string;
  language: string;
};

export type CheckinSettingsDto = {
  settings_version: number;
  check_in_time: string;
  check_out_time: string;
  check_in_latest_time: string | null;
  guest_checkin_opens_days_before: number;
};

export type AfterHoursArrivalPolicy = "contact" | "not_allowed";

export type AutomationSettingsDto = {
  settings_version: number;
  after_hours_arrival_policy: AfterHoursArrivalPolicy;
  after_hours_contact_phone: string;
  guest_arrival_auto_reply_enabled: boolean;
  guest_parking_auto_reply_enabled: boolean;
};

export const GENERAL_LANGUAGE_OPTIONS = ["hr", "en", "de", "it", "fr", "es"] as const;

export const AFTER_HOURS_POLICY_OPTIONS = ["contact", "not_allowed"] as const;

export function generalSettingsPath(propertyId: number): string {
  return `/api/stay/reception/properties/${propertyId}/settings/general/`;
}

export function checkinSettingsPath(propertyId: number): string {
  return `/api/stay/reception/properties/${propertyId}/settings/checkin/`;
}

export function automationSettingsPath(propertyId: number): string {
  return `/api/stay/reception/properties/${propertyId}/settings/automation/`;
}

export function emptyGeneralSettingsDraft(): GeneralSettingsDto {
  return {
    settings_version: 1,
    name: "",
    slug: "",
    address: "",
    timezone: "",
    language: "",
  };
}

export function emptyCheckinSettingsDraft(): CheckinSettingsDto {
  return {
    settings_version: 1,
    check_in_time: "15:00",
    check_out_time: "11:00",
    check_in_latest_time: null,
    guest_checkin_opens_days_before: 7,
  };
}

export function emptyAutomationSettingsDraft(): AutomationSettingsDto {
  return {
    settings_version: 1,
    after_hours_arrival_policy: "contact",
    after_hours_contact_phone: "",
    guest_arrival_auto_reply_enabled: true,
    guest_parking_auto_reply_enabled: true,
  };
}

/** Match Django ``settings_version_etag`` (weak ETag). */
export function settingsVersionEtag(version: number): string {
  return `W/"${Math.trunc(version)}"`;
}

/** Prefer response ETag; fall back to body ``settings_version`` if BFF omitted the header. */
export function etagFromSettingsResponse(
  res: Response,
  settingsVersion: number | null | undefined,
): string | null {
  const header = res.headers.get("ETag");
  if (header) {
    return header;
  }
  if (settingsVersion == null || !Number.isFinite(Number(settingsVersion))) {
    return null;
  }
  return settingsVersionEtag(Number(settingsVersion));
}

export function generalEditableSnapshot(draft: GeneralSettingsDto): string {
  return JSON.stringify({
    name: draft.name,
    address: draft.address,
    timezone: draft.timezone,
    language: draft.language,
  });
}

export function checkinEditableSnapshot(draft: CheckinSettingsDto): string {
  return JSON.stringify({
    check_in_time: draft.check_in_time,
    check_out_time: draft.check_out_time,
    check_in_latest_time: draft.check_in_latest_time,
    guest_checkin_opens_days_before: draft.guest_checkin_opens_days_before,
  });
}

export function automationEditableSnapshot(draft: AutomationSettingsDto): string {
  return JSON.stringify({
    after_hours_arrival_policy: draft.after_hours_arrival_policy,
    after_hours_contact_phone: draft.after_hours_contact_phone,
    guest_arrival_auto_reply_enabled: draft.guest_arrival_auto_reply_enabled,
    guest_parking_auto_reply_enabled: draft.guest_parking_auto_reply_enabled,
  });
}

export function isSettingsDirty(baseline: string | null, snapshot: string): boolean {
  return baseline != null && baseline !== snapshot;
}
