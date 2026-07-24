"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { GuestPortalSharePanel } from "@/app/settings/_components/GuestPortalSharePanel";
import { useSettingsShell } from "@/app/settings/_components/SettingsShell";
import {
  emptyGuestSettingsDraft,
  guestEditableSnapshot,
  guestSettingsPath,
  guestSettingsPreviewPath,
  validateWifiDraft,
  type GuestPortalPreview,
  type GuestSettingsDto,
} from "@/lib/guestSettings";
import { GUEST_SETTINGS_SCHEMA_VERSION } from "@/lib/guestSettingsLimits";
import { etagFromSettingsResponse, isSettingsDirty } from "@/lib/sectionSettings";
function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-sm font-medium text-stay-navy">{label}</span>
      {children}
    </label>
  );
}

function inputClassName() {
  return "w-full rounded border border-stay-border bg-white px-3 py-2 text-sm text-stay-navy";
}

export default function SettingsGuestPage() {
  const t = useTranslations("settings.guest");
  const { root, propertyId, loading: shellLoading } = useSettingsShell();

  const [draft, setDraft] = useState<GuestSettingsDto>(emptyGuestSettingsDraft());
  const [baseline, setBaseline] = useState<string | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [preview, setPreview] = useState<GuestPortalPreview | null>(null);
  const [previewLang, setPreviewLang] = useState("en");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedMsg, setSavedMsg] = useState("");

  const load = useCallback(async (id: number) => {
    setLoading(true);
    setError("");
    setSavedMsg("");
    try {
      const [settingsRes, previewRes] = await Promise.all([
        fetch(guestSettingsPath(id)),
        fetch(guestSettingsPreviewPath(id, { lang: previewLang })),
      ]);
      if (!settingsRes.ok) {
        throw new Error(t("loadFailed"));
      }
      const json = (await settingsRes.json()) as GuestSettingsDto;
      setDraft(json);
      setBaseline(guestEditableSnapshot(json));
      setEtag(etagFromSettingsResponse(settingsRes, json.settings_version));
      if (previewRes.ok) {
        setPreview((await previewRes.json()) as GuestPortalPreview);
      } else {
        setPreview(null);
      }
    } catch {
      setError(t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [previewLang, t]);

  useEffect(() => {
    if (propertyId == null || !root?.tabs.guest) return;
    void load(propertyId);
  }, [propertyId, root?.tabs.guest, load]);

  const refreshPreview = useCallback(async () => {
    if (propertyId == null) return;
    const res = await fetch(guestSettingsPreviewPath(propertyId, { lang: previewLang }));
    if (res.ok) {
      setPreview((await res.json()) as GuestPortalPreview);
    }
  }, [propertyId, previewLang]);

  useEffect(() => {
    if (propertyId == null || !root?.capabilities.preview) return;
    void refreshPreview();
  }, [previewLang, propertyId, refreshPreview, root?.capabilities.preview]);

  async function onSave() {
    if (propertyId == null || !etag) return;
    const wifiError = validateWifiDraft(draft.wifi);
    if (wifiError) {
      setError(wifiError);
      return;
    }
    setSaving(true);
    setError("");
    setSavedMsg("");
    try {
      const res = await fetch(guestSettingsPath(propertyId), {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "If-Match": etag,
        },
        body: JSON.stringify({
          schema_version: GUEST_SETTINGS_SCHEMA_VERSION,
          wifi: draft.wifi,
          parking: draft.parking,
          arrival: draft.arrival,
          breakfast: draft.breakfast,
          contact: draft.contact,
          self_service: draft.self_service,
          guide: draft.guide,
        }),
      });
      if (res.status === 409) {
        const body = await res.json();
        if (body.guest_settings) {
          const remote = body.guest_settings as GuestSettingsDto;
          setDraft(remote);
          setBaseline(guestEditableSnapshot(remote));
        }
        setEtag(
          etagFromSettingsResponse(
            res,
            (body.guest_settings as GuestSettingsDto | undefined)?.settings_version,
          ),
        );
        setError(t("conflict"));
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const detail = body?.detail || t("saveFailed");
        setError(typeof detail === "string" ? detail : t("saveFailed"));
        return;
      }
      const json = (await res.json()) as GuestSettingsDto;
      setDraft(json);
      setBaseline(guestEditableSnapshot(json));
      setEtag(etagFromSettingsResponse(res, json.settings_version));
      setSavedMsg(t("saved"));
      await refreshPreview();
    } catch {
      setError(t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (shellLoading || !root?.tabs.guest) return null;

  if (!root.capabilities.guest_settings) {
    return <p className="mt-4 text-sm text-stay-muted">{t("unavailable")}</p>;
  }

  if (propertyId == null) {
    return <p className="mt-4 text-sm text-stay-muted">{t("pickProperty")}</p>;
  }

  const dirty = isSettingsDirty(baseline, guestEditableSnapshot(draft));
  const arrivalEn = draft.arrival.texts.en ?? "";
  const breakfastEn = draft.breakfast.texts.en ?? "";

  return (
    <div className="mt-4 grid gap-6 lg:grid-cols-2">
      <section className="space-y-4">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-stay-navy">{t("title")}</h2>
          <button
            type="button"
            onClick={() => void onSave()}
            disabled={saving || loading || !etag || !dirty}
            className="rounded bg-stay-navy px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {saving ? t("saving") : t("save")}
          </button>
        </div>

        {loading ? <p className="text-sm text-stay-muted">{t("loading")}</p> : null}
        {error ? <p className="text-sm text-red-600">{error}</p> : null}
        {savedMsg ? <p className="text-sm text-green-700">{savedMsg}</p> : null}

        <div className="space-y-3 rounded border border-stay-border bg-white p-4">
          <h3 className="text-sm font-semibold text-stay-navy">{t("wifi")}</h3>
          <Field label={t("ssid")}>
            <input
              className={inputClassName()}
              value={draft.wifi.ssid}
              onChange={(e) =>
                setDraft((d) => ({ ...d, wifi: { ...d.wifi, ssid: e.target.value } }))
              }
            />
          </Field>
          <Field label={t("password")}>
            <input
              className={inputClassName()}
              value={draft.wifi.password}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  wifi: { ...d.wifi, password: e.target.value },
                }))
              }
            />
          </Field>
        </div>

        <div className="space-y-3 rounded border border-stay-border bg-white p-4">
          <h3 className="text-sm font-semibold text-stay-navy">{t("arrival")}</h3>
          <Field label={t("mapsUrl")}>
            <input
              className={inputClassName()}
              value={draft.arrival.maps_url}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  arrival: { ...d.arrival, maps_url: e.target.value },
                }))
              }
            />
          </Field>
          <Field label={t("arrivalTextEn")}>
            <textarea
              className={inputClassName()}
              rows={4}
              value={arrivalEn}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  arrival: {
                    ...d.arrival,
                    texts: { ...d.arrival.texts, en: e.target.value },
                  },
                }))
              }
            />
          </Field>
        </div>

        <div className="space-y-3 rounded border border-stay-border bg-white p-4">
          <h3 className="text-sm font-semibold text-stay-navy">{t("breakfast")}</h3>
          <Field label={t("breakfastHours")}>
            <input
              className={inputClassName()}
              value={draft.breakfast.hours}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  breakfast: { ...d.breakfast, hours: e.target.value },
                }))
              }
            />
          </Field>
          <Field label={t("breakfastTextEn")}>
            <textarea
              className={inputClassName()}
              rows={3}
              value={breakfastEn}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  breakfast: {
                    ...d.breakfast,
                    texts: { ...d.breakfast.texts, en: e.target.value },
                  },
                }))
              }
            />
          </Field>
        </div>

        <div className="space-y-3 rounded border border-stay-border bg-white p-4">
          <h3 className="text-sm font-semibold text-stay-navy">{t("contact")}</h3>
          <Field label={t("phone")}>
            <input
              className={inputClassName()}
              value={draft.contact.phone}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  contact: { ...d.contact, phone: e.target.value },
                }))
              }
            />
          </Field>
          <Field label={t("whatsapp")}>
            <input
              className={inputClassName()}
              value={draft.contact.whatsapp}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  contact: { ...d.contact, whatsapp: e.target.value },
                }))
              }
            />
          </Field>
        </div>

        <div className="space-y-3 rounded border border-stay-border bg-white p-4">
          <h3 className="text-sm font-semibold text-stay-navy">{t("selfService")}</h3>
          <Field label={t("selfServiceMode")}>
            <select
              className={inputClassName()}
              value={draft.self_service.mode}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  self_service: { ...d.self_service, mode: e.target.value },
                }))
              }
            >
              <option value="off">off</option>
              <option value="always">always</option>
              <option value="schedule">schedule</option>
              <option value="calendar">calendar</option>
            </select>
          </Field>
        </div>

        <p className="text-xs text-stay-muted">
          {t("versionLabel", { version: draft.settings_version })}
        </p>

        <GuestPortalSharePanel
          propertyId={propertyId}
          enabled={Boolean(root.capabilities.share)}
        />
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-stay-navy">{t("previewTitle")}</h2>
          <select
            className="rounded border border-stay-border bg-white px-2 py-1 text-sm"
            value={previewLang}
            onChange={(e) => setPreviewLang(e.target.value)}
          >
            {(draft.enabled_languages?.length
              ? draft.enabled_languages
              : ["en", "hr", "de"]
            ).map((lang) => (
              <option key={lang} value={lang}>
                {lang}
              </option>
            ))}
          </select>
        </div>
        {!root.capabilities.preview ? (
          <p className="text-sm text-stay-muted">{t("previewUnavailable")}</p>
        ) : preview ? (
          <div className="space-y-3 rounded border border-stay-border bg-white p-4 text-sm">
            <p className="font-medium text-stay-navy">{preview.property_name}</p>
            <p className="text-stay-muted">
              {t("previewSections")}: {preview.sections.join(", ")}
            </p>
            {preview.sections.map((section) => {
              const block = preview.content[section] as Record<string, unknown> | undefined;
              if (!block) return null;
              const text =
                typeof block.text === "string"
                  ? block.text
                  : typeof block.message === "string"
                    ? block.message
                    : null;
              return (
                <div key={section} className="border-t border-stay-border pt-3">
                  <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-stay-muted">
                    {section}
                  </p>
                  {text ? (
                    <p className="whitespace-pre-wrap text-stay-navy">{text}</p>
                  ) : (
                    <pre className="overflow-x-auto text-xs text-stay-muted">
                      {JSON.stringify(block, null, 2)}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-sm text-stay-muted">{t("previewEmpty")}</p>
        )}
      </section>
    </div>
  );
}
