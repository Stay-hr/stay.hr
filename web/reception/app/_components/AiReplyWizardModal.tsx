"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { extractApiError } from "@/lib/api-error";
import {
  ensureCorrelationId,
  logGuestMessageEvent,
  syncCorrelationIdFromResponse,
} from "@/lib/guestMessageDebug";
import type {
  GuestMessageComposeIntent,
  GuestMessageComposeResponse,
  GuestMessageComposeResult,
} from "@/lib/types";

const INTENTS: GuestMessageComposeIntent[] = ["reply", "checkin", "custom"];

type Props = {
  reservationId: number;
  onClose: () => void;
  onComplete: (result: GuestMessageComposeResult, intent: GuestMessageComposeIntent) => void;
};

function intentLabelKey(intent: GuestMessageComposeIntent): string {
  if (intent === "checkin") return "aiIntentCheckin";
  if (intent === "custom") return "aiIntentCustom";
  return "aiIntentReply";
}

function intentHelpKey(intent: GuestMessageComposeIntent): string {
  if (intent === "checkin") return "aiIntentCheckinHelp";
  if (intent === "custom") return "aiIntentCustomHelp";
  return "aiIntentReplyHelp";
}

export function AiReplyWizardModal({ reservationId, onClose, onComplete }: Props) {
  const t = useTranslations("guestMessages");
  const tc = useTranslations("common");
  const [step] = useState(1);
  const [intent, setIntent] = useState<GuestMessageComposeIntent>("reply");
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const stepTitles = [t("aiWizardStepInput")];
  const composeUrl = `/api/stay/reception/reservations/${reservationId}/messages/compose/`;

  function handleClose() {
    if (busy) return;
    onClose();
  }

  async function handleGenerate() {
    const cid = ensureCorrelationId(null);
    const composeStarted = performance.now();
    setBusy(true);
    setError("");
    logGuestMessageEvent("compose.start", {
      correlationId: cid,
      reservationId,
      composeIntent: intent,
    });
    try {
      const payload: Record<string, string> = { intent };
      if (intent !== "checkin" && hint.trim()) {
        payload.hint = hint.trim();
      }
      const res = await fetch(composeUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Correlation-Id": cid,
        },
        body: JSON.stringify(payload),
      });
      const echoedId = syncCorrelationIdFromResponse(res, cid);
      const composeMs = Math.round(performance.now() - composeStarted);
      if (!res.ok) {
        const message = await extractApiError(res, t("composeFailed"));
        logGuestMessageEvent("compose.error", {
          correlationId: echoedId,
          reservationId,
          composeIntent: intent,
          compose_ms: composeMs,
          httpStatus: res.status,
          error: message,
        });
        throw new Error(message);
      }
      const data = (await res.json()) as GuestMessageComposeResponse;
      logGuestMessageEvent("compose.success", {
        correlationId: echoedId,
        reservationId,
        composeIntent: intent,
        compose_ms: composeMs,
        draftId: data.draft_id,
        channels: data.channels,
      });
      const result: GuestMessageComposeResult = {
        draftId: data.draft_id,
        bodyText: data.body_text,
        channels: data.channels,
      };
      onComplete(result, intent);
    } catch (err) {
      setError(err instanceof Error ? err.message : tc("error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div
        className="card flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ai-reply-wizard-title"
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 id="ai-reply-wizard-title" className="font-semibold text-stay-navy">
            {t("aiWizardTitle")}
          </h2>
          <button
            type="button"
            className="btn-ghost px-2"
            onClick={handleClose}
            aria-label={tc("close")}
            disabled={busy}
          >
            ×
          </button>
        </div>

        <div className="border-b px-4 py-2">
          <ol className="flex flex-wrap gap-2 text-xs">
            {stepTitles.map((title, i) => {
              const n = i + 1;
              const active = n === step;
              return (
                <li key={title}>
                  <span
                    aria-current={active ? "step" : undefined}
                    className={`rounded-full px-2.5 py-1 ${
                      active ? "bg-stay-blue text-white" : "bg-slate-100 text-muted"
                    }`}
                  >
                    {n}. {title}
                  </span>
                </li>
              );
            })}
          </ol>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4">
          {step === 1 ? (
            <div className="space-y-3">
              <p className="text-sm font-medium">{t("aiWizardQuestion")}</p>
              <div className="space-y-2">
                {INTENTS.map((value) => {
                  const selected = intent === value;
                  return (
                    <button
                      key={value}
                      type="button"
                      className={`w-full rounded-lg border p-3 text-left ${
                        selected
                          ? "border-stay-blue bg-stay-blue/5 ring-1 ring-stay-blue"
                          : "border-stay-border hover:border-stay-blue/50"
                      }`}
                      onClick={() => setIntent(value)}
                      aria-pressed={selected}
                      disabled={busy}
                    >
                      <div className="font-medium text-stay-navy">{t(intentLabelKey(value))}</div>
                      <p className="mt-0.5 text-sm text-muted">{t(intentHelpKey(value))}</p>
                    </button>
                  );
                })}
              </div>
              {intent !== "checkin" ? (
                <label className="block text-sm">
                  <span className="mb-1 block text-muted">{t("aiHintLabel")}</span>
                  <input
                    className="input w-full"
                    value={hint}
                    onChange={(event) => setHint(event.target.value)}
                    placeholder={t("aiHintPlaceholder")}
                    disabled={busy}
                  />
                </label>
              ) : null}
              {error ? <p className="text-sm text-red-600">{error}</p> : null}
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap justify-end gap-2 border-t px-4 py-3">
          <button type="button" className="btn-ghost btn-sm" onClick={handleClose} disabled={busy}>
            {t("composerCancel")}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void handleGenerate()}
            disabled={busy}
          >
            {busy ? tc("loading") : t("aiGenerate")}
          </button>
        </div>
      </div>
    </div>
  );
}
