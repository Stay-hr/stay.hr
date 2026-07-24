"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { SettingsStubPanel } from "@/app/settings/_components/SettingsStubPanel";
import { useSettingsShell } from "@/app/settings/_components/SettingsShell";
import {
  GENERAL_LANGUAGE_OPTIONS,
  emptyGeneralSettingsDraft,
  etagFromSettingsResponse,
  generalEditableSnapshot,
  generalSettingsPath,
  isSettingsDirty,
  type GeneralSettingsDto,
} from "@/lib/sectionSettings";

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-sm font-medium text-stay-navy">{label}</span>
      {children}
      {hint ? <span className="block text-xs text-stay-muted">{hint}</span> : null}
    </label>
  );
}

function inputClassName() {
  return "w-full rounded border border-stay-border bg-white px-3 py-2 text-sm text-stay-navy";
}

export default function SettingsGeneralPage() {
  const t = useTranslations("settings.general");
  const { root, propertyId, loading: shellLoading } = useSettingsShell();

  const [draft, setDraft] = useState<GeneralSettingsDto>(emptyGeneralSettingsDraft());
  const [baseline, setBaseline] = useState<string | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedMsg, setSavedMsg] = useState("");

  const load = useCallback(
    async (id: number) => {
      setLoading(true);
      setError("");
      setSavedMsg("");
      try {
        const res = await fetch(generalSettingsPath(id));
        if (!res.ok) {
          throw new Error(t("loadFailed"));
        }
        const json = (await res.json()) as GeneralSettingsDto;
        setDraft(json);
        setBaseline(generalEditableSnapshot(json));
        setEtag(etagFromSettingsResponse(res, json.settings_version));
      } catch {
        setError(t("loadFailed"));
      } finally {
        setLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    if (propertyId == null || !root?.tabs.general) return;
    void load(propertyId);
  }, [propertyId, root?.tabs.general, load]);

  async function onSave() {
    if (propertyId == null || !etag) return;
    if (!draft.name.trim()) {
      setError(t("nameRequired"));
      return;
    }
    setSaving(true);
    setError("");
    setSavedMsg("");
    try {
      const res = await fetch(generalSettingsPath(propertyId), {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "If-Match": etag,
        },
        body: JSON.stringify({
          name: draft.name,
          address: draft.address,
          timezone: draft.timezone,
          language: draft.language,
        }),
      });
      if (res.status === 409) {
        const body = await res.json();
        if (body.general_settings) {
          const remote = body.general_settings as GeneralSettingsDto;
          setDraft(remote);
          setBaseline(generalEditableSnapshot(remote));
        }
        setEtag(
          etagFromSettingsResponse(
            res,
            (body.general_settings as GeneralSettingsDto | undefined)?.settings_version,
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
      const json = (await res.json()) as GeneralSettingsDto;
      setDraft(json);
      setBaseline(generalEditableSnapshot(json));
      setEtag(etagFromSettingsResponse(res, json.settings_version));
      setSavedMsg(t("saved"));
    } catch {
      setError(t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (shellLoading) return null;
  if (!root?.tabs.general) {
    return <SettingsStubPanel title={t("title")} message={t("unavailable")} />;
  }
  if (!root.capabilities.general) {
    return <p className="mt-4 text-sm text-stay-muted">{t("unavailable")}</p>;
  }
  if (propertyId == null) {
    return <p className="mt-4 text-sm text-stay-muted">{t("pickProperty")}</p>;
  }

  const dirty = isSettingsDirty(baseline, generalEditableSnapshot(draft));

  return (
    <section className="mt-4 max-w-xl space-y-4">
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
        <Field label={t("name")}>
          <input
            className={inputClassName()}
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
          />
        </Field>
        <Field label={t("slug")} hint={t("slugHint")}>
          <input className={inputClassName()} value={draft.slug} readOnly disabled />
        </Field>
        <Field label={t("address")}>
          <textarea
            className={inputClassName()}
            rows={3}
            value={draft.address}
            onChange={(e) => setDraft((d) => ({ ...d, address: e.target.value }))}
          />
        </Field>
        <Field label={t("timezone")} hint={t("timezoneHint")}>
          <input
            className={inputClassName()}
            value={draft.timezone}
            placeholder="Europe/Zagreb"
            onChange={(e) => setDraft((d) => ({ ...d, timezone: e.target.value }))}
          />
        </Field>
        <Field label={t("language")}>
          <select
            className={inputClassName()}
            value={draft.language}
            onChange={(e) => setDraft((d) => ({ ...d, language: e.target.value }))}
          >
            <option value="">{t("languageUnset")}</option>
            {GENERAL_LANGUAGE_OPTIONS.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <p className="text-xs text-stay-muted">
        {t("versionLabel", { version: draft.settings_version })}
      </p>
    </section>
  );
}
