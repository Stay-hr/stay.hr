/**
 * Mirrors backend/apps/properties/guest_settings_validation.py (ADR 0008).
 * Keep in sync when changing validation limits.
 */
export const GUEST_SETTINGS_SCHEMA_VERSION = 1;

export const MAX_WIFI_SSID_LENGTH = 128;
export const MAX_WIFI_PASSWORD_LENGTH = 128;
export const MAX_MAPS_URL_LENGTH = 2048;
export const MAX_CAPTION_LENGTH = 2000;
export const MAX_GUIDE_STEPS = 40;
export const MAX_PHONE_LENGTH = 32;
export const MAX_BREAKFAST_HOURS_LENGTH = 64;
export const MAX_PARKING_ZONE_LABEL_LENGTH = 255;
export const MAX_PARKING_PRICE_NOTES_LENGTH = 255;
export const MAX_TEXT_FIELD_LENGTH = 8000;

export const SUPPORTED_LANGUAGE_CODES = ["hr", "en", "de", "es", "fr", "sk"] as const;
export const ALLOWED_MAPS_URL_SCHEMES = ["http", "https"] as const;
