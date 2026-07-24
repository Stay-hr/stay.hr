import {
  GUEST_SETTINGS_SCHEMA_VERSION,
  MAX_WIFI_PASSWORD_LENGTH,
  MAX_WIFI_SSID_LENGTH,
} from "@/lib/guestSettingsLimits";

export type MediaRef = {
  asset_id: string | null;
  url: string;
};

export type GuestSettingsDto = {
  schema_version: number;
  settings_version: number;
  wifi: {
    ssid: string;
    password: string;
    instructions?: Record<string, string>;
  };
  parking: Record<string, unknown>;
  arrival: {
    texts: Record<string, string>;
    maps_url: string;
    entrance: { media: MediaRef };
  };
  breakfast: {
    texts: Record<string, string>;
    hours: string;
  };
  contact: {
    phone: string;
    whatsapp: string;
  };
  self_service: {
    mode: string;
    config: Record<string, unknown>;
  };
  guide: {
    sections?: Record<string, Record<string, string>>;
    order?: string[];
    enabled?: Record<string, boolean>;
    steps: Array<{
      section: string;
      caption: Record<string, string>;
      media: MediaRef;
    }>;
  };
  publication: {
    state: string;
    draft_available: boolean;
  };
  enabled_languages: string[];
};

export type GuestPortalPreview = {
  reservation_id: number | null;
  property_name: string;
  language: string;
  sections: string[];
  content: Record<string, unknown>;
  branding: Record<string, unknown>;
  self_service_active: boolean;
};

export function guestSettingsPath(propertyId: number): string {
  return `/api/stay/reception/properties/${propertyId}/settings/guest/`;
}

export function guestSettingsPreviewPath(
  propertyId: number,
  params?: { lang?: string; on_date?: string },
): string {
  const qs = new URLSearchParams();
  if (params?.lang) qs.set("lang", params.lang);
  if (params?.on_date) qs.set("on_date", params.on_date);
  const query = qs.toString();
  const base = `/api/stay/reception/properties/${propertyId}/settings/guest/preview/`;
  return query ? `${base}?${query}` : base;
}

export function guestSettingsSharePath(propertyId: number): string {
  return `/api/stay/reception/properties/${propertyId}/settings/share/`;
}

export type ShareKind = "portal";
export type ShareTarget = "reservation";
export type ShareChannel = "booking" | "whatsapp" | "email";

export type SharePortalRequest = {
  kind: ShareKind;
  target: ShareTarget;
  reservation_id: number;
  channel?: ShareChannel;
};

export type SharePortalResponse = {
  kind: ShareKind;
  target: ShareTarget;
  reservation_id: number;
  channel: ShareChannel | string;
  status: string;
  portal_url?: string;
  access_id?: number;
  draft_id?: number;
  url_draft_id?: number;
  reason?: string;
  error?: string;
};

export function emptyGuestSettingsDraft(): GuestSettingsDto {
  return {
    schema_version: GUEST_SETTINGS_SCHEMA_VERSION,
    settings_version: 1,
    wifi: { ssid: "", password: "" },
    parking: {},
    arrival: {
      texts: {},
      maps_url: "",
      entrance: { media: { asset_id: null, url: "" } },
    },
    breakfast: { texts: {}, hours: "" },
    contact: { phone: "", whatsapp: "" },
    self_service: { mode: "off", config: {} },
    guide: { steps: [] },
    publication: { state: "published", draft_available: false },
    enabled_languages: ["en", "hr"],
  };
}

export function validateWifiDraft(wifi: GuestSettingsDto["wifi"]): string | null {
  if (wifi.ssid.length > MAX_WIFI_SSID_LENGTH) {
    return `SSID must be at most ${MAX_WIFI_SSID_LENGTH} characters.`;
  }
  if (wifi.password.length > MAX_WIFI_PASSWORD_LENGTH) {
    return `Password must be at most ${MAX_WIFI_PASSWORD_LENGTH} characters.`;
  }
  return null;
}

/** Fields sent on PATCH — used for dirty detection (ignore settings_version etc.). */
export function guestEditableSnapshot(draft: GuestSettingsDto): string {
  return JSON.stringify({
    wifi: draft.wifi,
    parking: draft.parking,
    arrival: draft.arrival,
    breakfast: draft.breakfast,
    contact: draft.contact,
    self_service: draft.self_service,
    guide: draft.guide,
  });
}
