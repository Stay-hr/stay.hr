"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { SettingsStubPanel } from "@/app/settings/_components/SettingsStubPanel";
import { useSettingsShell } from "@/app/settings/_components/SettingsShell";
import {
  checkinEditableSnapshot,
  checkinSettingsPath,
  emptyCheckinSettingsDraft,
  etagFromSettingsResponse,
  isSettingsDirty,
  type CheckinSettingsDto,
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

export default function SettingsCheckinPage() {
  const t = useTranslations("settings.checkin");
  const { root, propertyId, loading: shellLoading } = useSettingsShell();

  const [draft, setDraft] = useState<CheckinSettingsDto>(emptyCheckinSettingsDraft());
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
        const res = await fetch(checkinSettingsPath(id));
        if (!res.ok) {
          throw new Error(t("loadFailed"));
        }
        const json = (await res.json()) as CheckinSettingsDto;
        setDraft(json);
        setBaseline(checkinEditableSnapshot(json));
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
    if (propertyId == null || !root?.tabs.checkin) return;
    void load(propertyId);
  }, [propertyId, root?.tabs.checkin, load]);

  async function onSave() {
    if (propertyId == null || !etag) return;
    setSaving(true);
    setError("");
    setSavedMsg("");
    try {
      const res = await fetch(checkinSettingsPath(propertyId), {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "If-Match": etag,
        },
        body: JSON.stringify({
          check_in_time: draft.check_in_time,
          check_out_time: draft.check_out_time,
          check_in_latest_time: draft.check_in_latest_time || null,
          guest_checkin_opens_days_before: draft.guest_checkin_opens_days_before,
        }),
      });
      if (res.status === 409) {
        const body = await res.json();
        if (body.checkin_settings) {
          const remote = body.checkin_settings as CheckinSettingsDto;
          setDraft(remote);
          setBaseline(checkinEditableSnapshot(remote));
        }
        setEtag(
          etagFromSettingsResponse(
            res,
            (body.checkin_settings as CheckinSettingsDto | undefined)?.settings_version,
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
      const json = (await res.json()) as CheckinSettingsDto;
      setDraft(json);
      setBaseline(checkinEditableSnapshot(json));
      setEtag(etagFromSettingsResponse(res, json.settings_version));
      setSavedMsg(t("saved"));
    } catch {
      setError(t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (shellLoading) return null;
  if (!root?.tabs.checkin) {
    return <SettingsStubPanel title={t("title")} message={t("unavailable")} />;
  }
  if (!root.capabilities.checkin) {
    return <p className="mt-4 text-sm text-stay-muted">{t("unavailable")}</p>;
  }
  if (propertyId == null) {
    return <p className="mt-4 text-sm text-stay-muted">{t("pickProperty")}</p>;
  }

  const dirty = isSettingsDirty(baseline, checkinEditableSnapshot(draft));

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
        <Field label={t("checkInTime")}>
          <input
            type="time"
            className={inputClassName()}
            value={draft.check_in_time}
            onChange={(e) =>
              setDraft((d) => ({ ...d, check_in_time: e.target.value }))
            }
          />
        </Field>
        <Field label={t("checkOutTime")}>
          <input
            type="time"
            className={inputClassName()}
            value={draft.check_out_time}
            onChange={(e) =>
              setDraft((d) => ({ ...d, check_out_time: e.target.value }))
            }
          />
        </Field>
        <Field label={t("checkInLatestTime")} hint={t("checkInLatestHint")}>
          <input
            type="time"
            className={inputClassName()}
            value={draft.check_in_latest_time ?? ""}
            onChange={(e) =>
              setDraft((d) => ({
                ...d,
                check_in_latest_time: e.target.value ? e.target.value : null,
              }))
            }
          />
        </Field>
        <Field
          label={t("opensDaysBefore")}
          hint={t("opensDaysBeforeHint")}
        >
          <input
            type="number"
            min={0}
            max={90}
            className={inputClassName()}
            value={draft.guest_checkin_opens_days_before}
            onChange={(e) =>
              setDraft((d) => ({
                ...d,
                guest_checkin_opens_days_before: Number(e.target.value) || 0,
              }))
            }
          />
        </Field>
      </div>

      <p className="text-xs text-stay-muted">
        {t("versionLabel", { version: draft.settings_version })}
      </p>
    </section>
  );
}
