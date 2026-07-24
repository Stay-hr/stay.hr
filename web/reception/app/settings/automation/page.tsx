"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { SettingsStubPanel } from "@/app/settings/_components/SettingsStubPanel";
import { useSettingsShell } from "@/app/settings/_components/SettingsShell";
import {
  AFTER_HOURS_POLICY_OPTIONS,
  automationEditableSnapshot,
  automationSettingsPath,
  emptyAutomationSettingsDraft,
  etagFromSettingsResponse,
  isSettingsDirty,
  type AfterHoursArrivalPolicy,
  type AutomationSettingsDto,
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

export default function SettingsAutomationPage() {
  const t = useTranslations("settings.automation");
  const { root, propertyId, loading: shellLoading } = useSettingsShell();

  const [draft, setDraft] = useState<AutomationSettingsDto>(emptyAutomationSettingsDraft());
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
        const res = await fetch(automationSettingsPath(id));
        if (!res.ok) {
          throw new Error(t("loadFailed"));
        }
        const json = (await res.json()) as AutomationSettingsDto;
        setDraft(json);
        setBaseline(automationEditableSnapshot(json));
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
    if (propertyId == null || !root?.tabs.automation) return;
    void load(propertyId);
  }, [propertyId, root?.tabs.automation, load]);

  async function onSave() {
    if (propertyId == null || !etag) return;
    setSaving(true);
    setError("");
    setSavedMsg("");
    try {
      const res = await fetch(automationSettingsPath(propertyId), {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "If-Match": etag,
        },
        body: JSON.stringify({
          after_hours_arrival_policy: draft.after_hours_arrival_policy,
          after_hours_contact_phone: draft.after_hours_contact_phone,
          guest_arrival_auto_reply_enabled: draft.guest_arrival_auto_reply_enabled,
          guest_parking_auto_reply_enabled: draft.guest_parking_auto_reply_enabled,
        }),
      });
      if (res.status === 409) {
        const body = await res.json();
        if (body.automation_settings) {
          const remote = body.automation_settings as AutomationSettingsDto;
          setDraft(remote);
          setBaseline(automationEditableSnapshot(remote));
        }
        setEtag(
          etagFromSettingsResponse(
            res,
            (body.automation_settings as AutomationSettingsDto | undefined)?.settings_version,
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
      const json = (await res.json()) as AutomationSettingsDto;
      setDraft(json);
      setBaseline(automationEditableSnapshot(json));
      setEtag(etagFromSettingsResponse(res, json.settings_version));
      setSavedMsg(t("saved"));
    } catch {
      setError(t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (shellLoading) return null;
  if (!root?.tabs.automation) {
    return <SettingsStubPanel title={t("title")} message={t("unavailable")} />;
  }
  if (!root.capabilities.automation) {
    return <p className="mt-4 text-sm text-stay-muted">{t("unavailable")}</p>;
  }
  if (propertyId == null) {
    return <p className="mt-4 text-sm text-stay-muted">{t("pickProperty")}</p>;
  }

  const dirty = isSettingsDirty(baseline, automationEditableSnapshot(draft));

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
        <h3 className="text-sm font-semibold text-stay-navy">{t("afterHoursHeading")}</h3>
        <Field label={t("afterHoursPolicy")} hint={t("afterHoursPolicyHint")}>
          <select
            className={inputClassName()}
            value={draft.after_hours_arrival_policy}
            onChange={(e) =>
              setDraft((d) => ({
                ...d,
                after_hours_arrival_policy: e.target.value as AfterHoursArrivalPolicy,
              }))
            }
          >
            {AFTER_HOURS_POLICY_OPTIONS.map((code) => (
              <option key={code} value={code}>
                {t(`policy.${code}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("afterHoursPhone")} hint={t("afterHoursPhoneHint")}>
          <input
            type="tel"
            className={inputClassName()}
            value={draft.after_hours_contact_phone}
            onChange={(e) =>
              setDraft((d) => ({ ...d, after_hours_contact_phone: e.target.value }))
            }
            disabled={draft.after_hours_arrival_policy === "not_allowed"}
          />
        </Field>
      </div>

      <div className="space-y-3 rounded border border-stay-border bg-white p-4">
        <h3 className="text-sm font-semibold text-stay-navy">{t("autoRepliesHeading")}</h3>
        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            className="mt-1"
            checked={draft.guest_arrival_auto_reply_enabled}
            onChange={(e) =>
              setDraft((d) => ({
                ...d,
                guest_arrival_auto_reply_enabled: e.target.checked,
              }))
            }
          />
          <span>
            <span className="block text-sm font-medium text-stay-navy">
              {t("arrivalAutoReply")}
            </span>
            <span className="block text-xs text-stay-muted">{t("arrivalAutoReplyHint")}</span>
          </span>
        </label>
        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            className="mt-1"
            checked={draft.guest_parking_auto_reply_enabled}
            onChange={(e) =>
              setDraft((d) => ({
                ...d,
                guest_parking_auto_reply_enabled: e.target.checked,
              }))
            }
          />
          <span>
            <span className="block text-sm font-medium text-stay-navy">
              {t("parkingAutoReply")}
            </span>
            <span className="block text-xs text-stay-muted">{t("parkingAutoReplyHint")}</span>
          </span>
        </label>
      </div>

      <p className="text-xs text-stay-muted">
        {t("versionLabel", { version: draft.settings_version })}
      </p>
    </section>
  );
}
