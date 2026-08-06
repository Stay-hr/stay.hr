"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import type {
  FieldConfidence,
  GuestCheckInGuestFields,
  GuestCheckInJobResponse,
  GuestCheckInSessionResponse,
  GuestCheckInSlot,
} from "@/lib/types";

type Props = {
  token: string;
};

type EntryMode = "choose" | "photo" | "manual" | "form";

const AUTOSAVE_MS = 500;
const OCR_POLL_MS = 1500;
const OCR_TERMINAL = new Set(["applied", "failed"]);

function emptyGuest(): GuestCheckInGuestFields {
  return {
    first_name: "",
    last_name: "",
    email: "",
    phone: "",
    date_of_birth: null,
    document_number: "",
    nationality: "",
    sex: "",
    address: "",
    document_type: "",
  };
}

function guestFromSlot(slot: GuestCheckInSlot): GuestCheckInGuestFields {
  return {
    first_name: slot.guest.first_name || "",
    last_name: slot.guest.last_name || "",
    email: slot.guest.email || "",
    phone: slot.guest.phone || "",
    date_of_birth: slot.guest.date_of_birth || null,
    document_number: slot.guest.document_number || "",
    nationality: slot.guest.nationality || "",
    sex: slot.guest.sex || "",
    address: slot.guest.address || "",
    document_type: slot.guest.document_type || "",
  };
}

function confidenceClass(level: string | undefined): string {
  if (level === "low") return "border-amber-400 bg-amber-50";
  if (level === "medium") return "border-yellow-300 bg-yellow-50";
  return "";
}

function hasText(value: string | null | undefined): boolean {
  return Boolean((value || "").trim());
}

function hasGender(sex: string | null | undefined): boolean {
  const raw = (sex || "").trim().toLowerCase();
  return raw === "male" || raw === "female" || raw === "m" || raw === "f";
}

/** Mirrors backend GuestValidator required fields for slot readiness. */
function localMissingFields(form: GuestCheckInGuestFields): string[] {
  const missing: string[] = [];
  if (!hasText(form.first_name)) missing.push("first_name");
  if (!hasText(form.last_name)) missing.push("last_name");
  if (!form.date_of_birth) missing.push("date_of_birth");
  if (!hasText(form.nationality)) missing.push("nationality");
  if (!hasGender(form.sex)) missing.push("sex");
  if (!hasText(form.document_number)) missing.push("document_number");
  if (!hasText(form.document_type)) missing.push("document_type");
  if (!hasText(form.address)) missing.push("address");
  return missing;
}

function isFormReady(form: GuestCheckInGuestFields): boolean {
  return localMissingFields(form).length === 0;
}

export function GuestCheckInWizard({ token }: Props) {
  const t = useTranslations("checkIn");
  const tc = useTranslations("common");
  const [session, setSession] = useState<GuestCheckInSessionResponse | null>(null);
  const [step, setStep] = useState(0);
  const [entryMode, setEntryMode] = useState<EntryMode>("choose");
  const [form, setForm] = useState<GuestCheckInGuestFields>(emptyGuest());
  const [fieldConfidence, setFieldConfidence] = useState<FieldConfidence>({});
  const [photoDocType, setPhotoDocType] = useState<"identity_card" | "passport">("identity_card");
  const [photoFront, setPhotoFront] = useState<File | null>(null);
  const [photoBack, setPhotoBack] = useState<File | null>(null);
  const [ocrJobId, setOcrJobId] = useState<number | null>(null);
  const [ocrScanning, setOcrScanning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [error, setError] = useState("");
  const [gateStatus, setGateStatus] = useState("");
  const [completed, setCompleted] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const formRef = useRef(form);

  formRef.current = form;

  const loadSession = useCallback(async () => {
    const res = await fetch(`/api/check-in/${encodeURIComponent(token)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const status = (data.status as string) || "";
      if (status) setGateStatus(status);
      throw new Error((data.error as string) || t("loadFailed"));
    }
    return data as GuestCheckInSessionResponse;
  }, [token, t]);

  useEffect(() => {
    let cancelled = false;
    void loadSession()
      .then((data) => {
        if (cancelled) return;
        setSession(data);
        if (data.slots.length > 0) {
          const slot = data.slots[0];
          setForm(guestFromSlot(slot));
          setFieldConfidence(slot.field_confidence || {});
          setEntryMode(slot.status === "ready" ? "form" : "choose");
        }
        setError("");
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : tc("error"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadSession, tc]);

  const currentSlot = session?.slots[step] ?? null;
  const isSlotReady = currentSlot?.status === "ready";
  const formReady = isFormReady(form);
  const canProceed = formReady || isSlotReady;
  const missingForHint = formReady ? [] : localMissingFields(form);

  const resetSlotUi = useCallback((slot: GuestCheckInSlot) => {
    setForm(guestFromSlot(slot));
    setFieldConfidence(slot.field_confidence || {});
    setPhotoFront(null);
    setPhotoBack(null);
    setOcrJobId(null);
    setOcrScanning(false);
    setEntryMode(slot.status === "ready" ? "form" : "choose");
  }, []);

  const patchSlot = useCallback(
    async (
      position: number,
      fields: GuestCheckInGuestFields,
    ): Promise<{ ok: boolean; slot: GuestCheckInSlot | null; canComplete: boolean }> => {
      setSaving(true);
      try {
        const res = await fetch(
          `/api/check-in/${encodeURIComponent(token)}/slots/${position}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(fields),
          },
        );
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error((data.error as string) || (data.detail as string) || t("saveFailed"));
        }
        const slot = data.slot as GuestCheckInSlot;
        const canComplete = Boolean(data.can_complete);
        setSession((prev) => {
          if (!prev) return prev;
          const slots = prev.slots.map((s) => (s.position === slot.position ? { ...s, ...slot } : s));
          return {
            ...prev,
            status: data.status as string,
            effective_status: data.effective_status as string,
            required_slots: data.required_slots as number,
            ready_slots: data.ready_slots as number,
            can_complete: canComplete,
            ops_version: typeof data.ops_version === "number" ? data.ops_version : prev.ops_version,
            slots,
          };
        });
        if (data.slot?.field_confidence) {
          setFieldConfidence(data.slot.field_confidence as FieldConfidence);
        }
        setError("");
        return { ok: true, slot, canComplete };
      } catch (err) {
        setError(err instanceof Error ? err.message : t("saveFailed"));
        return { ok: false, slot: null, canComplete: false };
      } finally {
        setSaving(false);
      }
    },
    [token, t],
  );

  const applySessionPayload = useCallback(
    (data: GuestCheckInSessionResponse) => {
      setSession(data);
      setStep((prev) => {
        if (data.slots.length === 0) return 0;
        const nextStep = Math.min(prev, data.slots.length - 1);
        const slot = data.slots[nextStep];
        if (slot) {
          setForm(guestFromSlot(slot));
          setFieldConfidence(slot.field_confidence || {});
          setPhotoFront(null);
          setPhotoBack(null);
          setOcrJobId(null);
          setOcrScanning(false);
          setEntryMode(slot.status === "ready" ? "form" : "choose");
        }
        return nextStep;
      });
    },
    [],
  );

  const commitSlot = useCallback(
    async (
      position: number,
      opsVersion: number,
    ): Promise<{ ok: boolean; slot: GuestCheckInSlot | null; canComplete: boolean }> => {
      setSaving(true);
      try {
        const res = await fetch(
          `/api/check-in/${encodeURIComponent(token)}/slots/${position}/commit`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ops_version: opsVersion }),
          },
        );
        const data = await res.json().catch(() => ({}));
        if (res.status === 409 && data.status === "session_conflict") {
          const refreshed = await loadSession();
          applySessionPayload(refreshed);
          throw new Error(t("sessionConflict"));
        }
        if (!res.ok) {
          const serverMissing = Array.isArray(data.missing_fields)
            ? (data.missing_fields as string[])
            : [];
          const fieldErrors = (data.field_errors || {}) as Record<string, string>;
          if (fieldErrors.address) {
            throw new Error(fieldErrors.address);
          }
          if (serverMissing.length > 0) {
            const labels = serverMissing
              .map((key) => {
                try {
                  return t(`fieldLabels.${key}` as "fieldLabels.first_name");
                } catch {
                  return key;
                }
              })
              .join(", ");
            throw new Error(
              `${t("validationHint")} ${t("validationSummary", { fields: labels })}`,
            );
          }
          throw new Error((data.detail as string) || (data.error as string) || t("commitFailed"));
        }
        const slot = data.slot as GuestCheckInSlot;
        const canComplete = Boolean(data.can_complete);
        setSession((prev) => {
          if (!prev) return prev;
          const slots = prev.slots.map((s) => (s.position === slot.position ? { ...s, ...slot } : s));
          return {
            ...prev,
            status: data.status as string,
            effective_status: data.effective_status as string,
            required_slots: data.required_slots as number,
            ready_slots: data.ready_slots as number,
            can_complete: canComplete,
            ops_version: typeof data.ops_version === "number" ? data.ops_version : prev.ops_version,
            expected_checkin_adults:
              data.expected_checkin_adults !== undefined
                ? (data.expected_checkin_adults as number | null)
                : prev.expected_checkin_adults,
            adults_count:
              data.adults_count !== undefined
                ? (data.adults_count as number | null)
                : prev.adults_count,
            slots,
          };
        });
        setError("");
        return { ok: true, slot, canComplete };
      } catch (err) {
        setError(err instanceof Error ? err.message : t("commitFailed"));
        return { ok: false, slot: null, canComplete: false };
      } finally {
        setSaving(false);
      }
    },
    [token, t, loadSession, applySessionPayload],
  );

  const patchOccupancy = useCallback(
    async (expectedCheckinAdults: number | null) => {
      if (!session) return;
      setSaving(true);
      setError("");
      try {
        const res = await fetch(`/api/check-in/${encodeURIComponent(token)}/occupancy`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_checkin_adults: expectedCheckinAdults,
            ops_version: session.ops_version,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 409 && data.status === "session_conflict") {
          const refreshed = await loadSession();
          applySessionPayload(refreshed);
          throw new Error(t("sessionConflict"));
        }
        if (!res.ok) {
          throw new Error((data.detail as string) || (data.error as string) || t("saveFailed"));
        }
        applySessionPayload(data as GuestCheckInSessionResponse);
      } catch (err) {
        setError(err instanceof Error ? err.message : t("saveFailed"));
      } finally {
        setSaving(false);
      }
    },
    [session, token, t, loadSession, applySessionPayload],
  );

  const applyJobResult = useCallback((data: GuestCheckInJobResponse) => {
    if (data.identity_status === "duplicate_identity") {
      setError(
        data.detail ||
          (data.existing_guest_name
            ? `Document already belongs to ${data.existing_guest_name}`
            : t("scanFailed")),
      );
      setEntryMode("form");
      return;
    }
    if (data.identity_status === "already_processed") {
      setError(data.detail || "Document already processed for this guest");
    } else if (data.identity_status === "mrz_inconsistent") {
      setError(data.detail || t("scanFailed"));
      setEntryMode("form");
      return;
    } else {
      setError("");
    }
    if (data.field_confidence) {
      setFieldConfidence(data.field_confidence);
    }
    const guest = data.slot?.guest || data.guest_preview;
    if (guest) {
      setForm({
        first_name: guest.first_name || "",
        last_name: guest.last_name || "",
        email: guest.email || "",
        phone: guest.phone || "",
        date_of_birth: guest.date_of_birth || null,
        document_number: guest.document_number || "",
        nationality: guest.nationality || "",
        sex: guest.sex || "",
        address: guest.address || "",
        document_type: guest.document_type || "",
      });
    }
    if (data.slot && session) {
      setSession((prev) => {
        if (!prev) return prev;
        const slots = prev.slots.map((s) =>
          s.position === data.slot!.position ? { ...s, ...data.slot! } : s,
        );
        return {
          ...prev,
          effective_status: data.effective_status || prev.effective_status,
          ready_slots: data.ready_slots ?? prev.ready_slots,
          can_complete: data.can_complete ?? prev.can_complete,
          slots,
        };
      });
    }
    setEntryMode("form");
  }, [session, t]);

  const pollJob = useCallback(
    async (jobId: number) => {
      const res = await fetch(
        `/api/check-in/${encodeURIComponent(token)}/jobs/${jobId}`,
      );
      const data = (await res.json().catch(() => ({}))) as GuestCheckInJobResponse & {
        detail?: string;
        error?: string;
      };
      // 409 duplicate_identity still carries a useful payload for the UI.
      if (!res.ok && data.identity_status !== "duplicate_identity") {
        throw new Error(data.detail || data.error || t("scanFailed"));
      }
      return data;
    },
    [token, t],
  );

  useEffect(() => {
    if (!ocrJobId || !ocrScanning) return undefined;

    let cancelled = false;
    const tick = async () => {
      try {
        const data = await pollJob(ocrJobId);
        if (cancelled) return;
        if (OCR_TERMINAL.has(data.status)) {
          setOcrScanning(false);
          if (data.status === "failed") {
            setError(data.error_message || t("scanFailed"));
            return;
          }
          applyJobResult(data);
          setError("");
        }
      } catch (err) {
        if (cancelled) return;
        setOcrScanning(false);
        setError(err instanceof Error ? err.message : t("scanFailed"));
      }
    };

    void tick();
    const timer = setInterval(() => void tick(), OCR_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [ocrJobId, ocrScanning, pollJob, applyJobResult, t]);

  const scheduleAutosave = useCallback(
    (position: number) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        void patchSlot(position, formRef.current);
      }, AUTOSAVE_MS);
    },
    [patchSlot],
  );

  useEffect(() => {
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
  }, []);

  function updateField<K extends keyof GuestCheckInGuestFields>(key: K, value: GuestCheckInGuestFields[K]) {
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      // Manual and post-OCR form modes both need autosave so the slot can become "ready".
      if (currentSlot && (entryMode === "form" || entryMode === "manual")) {
        scheduleAutosave(currentSlot.position);
      }
      return next;
    });
  }

  function formatMissingFields(fields: string[]): string {
    return fields
      .map((key) => {
        try {
          return t(`fieldLabels.${key}` as "fieldLabels.first_name");
        } catch {
          return key;
        }
      })
      .join(", ");
  }

  async function onNext(e: FormEvent) {
    e.preventDefault();
    if (!session || !currentSlot) return;

    const missing = localMissingFields(form);
    if (missing.length > 0) {
      setError(
        `${t("validationHint")} ${t("validationSummary", { fields: formatMissingFields(missing) })}`,
      );
      return;
    }

    if (saveTimer.current) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }

    const patched = await patchSlot(currentSlot.position, form);
    if (!patched.ok) return;

    const result = await commitSlot(currentSlot.position, session.ops_version);
    if (!result.ok) return;

    const slotReady = result.slot?.status === "ready";
    if (!slotReady) {
      const serverMissing = result.slot?.missing_fields?.length
        ? result.slot.missing_fields
        : localMissingFields(form);
      setError(
        `${t("validationHint")} ${t("validationSummary", {
          fields: formatMissingFields(serverMissing),
        })}`,
      );
      return;
    }

    if (step < session.slots.length - 1) {
      const nextStep = step + 1;
      setStep(nextStep);
      resetSlotUi(session.slots[nextStep]);
    }
  }

  async function onComplete() {
    if (!session?.can_complete) return;
    setCompleting(true);
    setError("");
    try {
      const res = await fetch(`/api/check-in/${encodeURIComponent(token)}/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ops_version: session.ops_version }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 409 && data.status === "session_conflict") {
        const refreshed = await loadSession();
        applySessionPayload(refreshed);
        throw new Error(t("sessionConflict"));
      }
      if (!res.ok) {
        throw new Error((data.detail as string) || (data.error as string) || t("completeFailed"));
      }
      setCompleted(true);
      setSession((prev) =>
        prev
          ? {
              ...prev,
              status: data.status as string,
              effective_status: data.effective_status as string,
              can_complete: false,
              ops_version:
                typeof data.ops_version === "number" ? data.ops_version : prev.ops_version,
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : t("completeFailed"));
    } finally {
      setCompleting(false);
    }
  }

  async function onUploadDocuments() {
    if (!currentSlot) return;
    if (!photoFront) {
      setError(t("scanFailed"));
      return;
    }
    if (photoDocType === "identity_card" && !photoBack) {
      setError(t("scanFailed"));
      return;
    }

    setError("");
    setOcrScanning(true);
    const body = new FormData();
    body.append("files", photoFront);
    if (photoBack) body.append("files", photoBack);

    try {
      const res = await fetch(
        `/api/check-in/${encodeURIComponent(token)}/slots/${currentSlot.position}/documents`,
        { method: "POST", body },
      );
      const data = (await res.json().catch(() => ({}))) as GuestCheckInJobResponse & {
        detail?: string;
      };
      if (!res.ok) {
        throw new Error(data.detail || t("scanFailed"));
      }
      setOcrJobId(data.job_id);
      if (OCR_TERMINAL.has(data.status)) {
        setOcrScanning(false);
        if (data.status === "failed") {
          throw new Error(data.error_message || t("scanFailed"));
        }
        const polled = await pollJob(data.job_id);
        applyJobResult(polled);
      }
    } catch (err) {
      setOcrScanning(false);
      setError(err instanceof Error ? err.message : t("scanFailed"));
    }
  }

  function confidenceHint(field: keyof GuestCheckInGuestFields): string | null {
    const level = fieldConfidence[field];
    if (level === "low") return t("confidenceLow");
    if (level === "medium") return t("confidenceMedium");
    return null;
  }

  const progressLabel = useMemo(() => {
    if (!session) return "";
    return t("progress", {
      ready: session.ready_slots,
      required: session.required_slots,
    });
  }, [session, t]);

  if (loading) {
    return (
      <div className="card text-center">
        <p className="text-muted">{t("loading")}</p>
      </div>
    );
  }

  if (gateStatus === "not_open_yet") {
    return (
      <div className="card space-y-3 text-center">
        <h1 className="text-xl font-bold text-stay-navy">{t("notOpenTitle")}</h1>
        <p className="text-muted">{t("notOpenBody")}</p>
      </div>
    );
  }

  if (gateStatus === "completed" || completed) {
    return (
      <div className="card space-y-3 text-center">
        <h1 className="text-xl font-bold text-stay-blue">{t("completedTitle")}</h1>
        <p className="text-muted">{t("completedBody")}</p>
      </div>
    );
  }

  if (gateStatus === "expired" || gateStatus === "revoked") {
    return (
      <div className="card space-y-3 text-center">
        <h1 className="text-xl font-bold text-stay-navy">{t("expiredTitle")}</h1>
        <p className="text-muted">{t("expiredBody")}</p>
      </div>
    );
  }

  if (!session || !currentSlot) {
    return (
      <div className="card text-center">
        <p className="text-red-600">{error || t("loadFailed")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="card space-y-2">
        <h1 className="text-2xl font-bold text-stay-navy">{t("title")}</h1>
        <p className="text-sm text-muted">
          {session.property_name} · {session.booking_code}
        </p>
        <p className="text-sm text-muted">
          {session.check_in} → {session.check_out}
        </p>
        <div className="flex items-center justify-between gap-3 pt-2">
          <span className="text-sm font-medium text-stay-navy">{progressLabel}</span>
          {session.effective_status === "ready" ? (
            <span className="badge bg-stay-blue-light text-stay-blue">{t("statusReady")}</span>
          ) : (
            <span className="badge">{t("statusPartial")}</span>
          )}
        </div>
        {session.required_slots > 1 ? (
          <button
            type="button"
            className="text-sm text-stay-blue underline"
            disabled={saving}
            onClick={() => void patchOccupancy(1)}
          >
            {t("travelingAlone")}
          </button>
        ) : null}
        {session.expected_checkin_adults != null ? (
          <button
            type="button"
            className="text-sm text-muted underline"
            disabled={saving}
            onClick={() => void patchOccupancy(null)}
          >
            {t("resetOccupancy")}
          </button>
        ) : null}
      </div>

      {entryMode === "choose" ? (
        <div className="card space-y-4">
          <h2 className="text-lg font-semibold">
            {t("guestStep", { current: step + 1, total: session.slots.length })}
          </h2>
          <p className="text-muted">{t("entryModeTitle")}</p>
          {error ? <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <button type="button" className="btn" onClick={() => setEntryMode("photo")}>
              {t("entryModePhoto")}
            </button>
            <button
              type="button"
              className="btn-ghost border border-stay-navy/20"
              onClick={() => setEntryMode("manual")}
            >
              {t("entryModeManual")}
            </button>
          </div>
        </div>
      ) : null}

      {entryMode === "photo" ? (
        <div className="card space-y-4">
          <h2 className="text-lg font-semibold">{t("photoTitle")}</h2>
          <p className="text-sm text-muted">
            {photoDocType === "passport" ? t("photoHintPassport") : t("photoHintId")}
          </p>
          {error ? <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
          <div>
            <label className="label" htmlFor="photo_document_type">
              {t("documentType")}
            </label>
            <select
              id="photo_document_type"
              className="input mt-1"
              value={photoDocType}
              onChange={(e) => {
                setPhotoDocType(e.target.value as "identity_card" | "passport");
                setPhotoFront(null);
                setPhotoBack(null);
              }}
            >
              <option value="identity_card">{t("docIdentityCard")}</option>
              <option value="passport">{t("docPassport")}</option>
            </select>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="photo_front">
                {photoDocType === "passport" ? t("photoPassport") : t("photoFront")}
              </label>
              <input
                id="photo_front"
                type="file"
                accept="image/*"
                capture="environment"
                className="input mt-1"
                onChange={(e) => setPhotoFront(e.target.files?.[0] ?? null)}
              />
            </div>
            {photoDocType === "identity_card" ? (
              <div>
                <label className="label" htmlFor="photo_back">
                  {t("photoBack")}
                </label>
                <input
                  id="photo_back"
                  type="file"
                  accept="image/*"
                  capture="environment"
                  className="input mt-1"
                  onChange={(e) => setPhotoBack(e.target.files?.[0] ?? null)}
                />
              </div>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className="btn-ghost"
              onClick={() => {
                setError("");
                setEntryMode("choose");
              }}
              disabled={ocrScanning}
            >
              {t("back")}
            </button>
            <button
              type="button"
              className="btn"
              disabled={ocrScanning || !photoFront || (photoDocType === "identity_card" && !photoBack)}
              onClick={() => void onUploadDocuments()}
            >
              {ocrScanning ? t("scanning") : t("uploadAndScan")}
            </button>
            <button
              type="button"
              className="btn-ghost"
              disabled={ocrScanning}
              onClick={() => {
                setError("");
                setEntryMode("manual");
              }}
            >
              {t("switchToManual")}
            </button>
          </div>
        </div>
      ) : null}

      {entryMode === "manual" || entryMode === "form" ? (
        <form onSubmit={(e) => void onNext(e)} className="card space-y-4">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">
              {t("guestStep", { current: step + 1, total: session.slots.length })}
            </h2>
            {isSlotReady ? (
              <span className="text-xs font-semibold uppercase tracking-wide text-stay-blue">
                {t("slotReady")}
              </span>
            ) : null}
          </div>

          {entryMode === "manual" ? (
            <button
              type="button"
              className="text-sm text-stay-blue underline"
              onClick={() => setEntryMode("choose")}
            >
              {t("back")}
            </button>
          ) : null}

          {error ? <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
          {!error && missingForHint.length > 0 ? (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">
              {t("validationHint")}{" "}
              {t("validationSummary", { fields: formatMissingFields(missingForHint) })}
            </p>
          ) : null}
          {saving ? <p className="text-xs text-muted">{t("saving")}</p> : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="first_name">
                {t("firstName")}
              </label>
              <input
                id="first_name"
                className={`input mt-1 ${confidenceClass(fieldConfidence.first_name)}`}
                value={form.first_name}
                onChange={(e) => updateField("first_name", e.target.value)}
                required
              />
              {confidenceHint("first_name") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("first_name")}</p>
              ) : null}
            </div>
            <div>
              <label className="label" htmlFor="last_name">
                {t("lastName")}
              </label>
              <input
                id="last_name"
                className={`input mt-1 ${confidenceClass(fieldConfidence.last_name)}`}
                value={form.last_name}
                onChange={(e) => updateField("last_name", e.target.value)}
                required
              />
              {confidenceHint("last_name") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("last_name")}</p>
              ) : null}
            </div>
            <div>
              <label className="label" htmlFor="date_of_birth">
                {t("dateOfBirth")}
              </label>
              <input
                id="date_of_birth"
                type="date"
                className={`input mt-1 ${confidenceClass(fieldConfidence.date_of_birth)}`}
                value={form.date_of_birth || ""}
                onChange={(e) => updateField("date_of_birth", e.target.value || null)}
                required
              />
              {confidenceHint("date_of_birth") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("date_of_birth")}</p>
              ) : null}
            </div>
            <div>
              <label className="label" htmlFor="nationality">
                {t("nationality")}
              </label>
              <input
                id="nationality"
                className={`input mt-1 ${confidenceClass(fieldConfidence.nationality)}`}
                maxLength={2}
                placeholder="HR"
                value={form.nationality}
                onChange={(e) => updateField("nationality", e.target.value.toUpperCase())}
                required
              />
              {confidenceHint("nationality") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("nationality")}</p>
              ) : null}
            </div>
            <div>
              <label className="label" htmlFor="sex">
                {t("sex")}
              </label>
              <select
                id="sex"
                className={`input mt-1 ${confidenceClass(fieldConfidence.sex)}`}
                value={form.sex}
                onChange={(e) => updateField("sex", e.target.value)}
                required
              >
                <option value="">{t("select")}</option>
                <option value="female">{t("sexFemale")}</option>
                <option value="male">{t("sexMale")}</option>
              </select>
              {confidenceHint("sex") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("sex")}</p>
              ) : null}
            </div>
            <div>
              <label className="label" htmlFor="document_type">
                {t("documentType")}
              </label>
              <select
                id="document_type"
                className={`input mt-1 ${confidenceClass(fieldConfidence.document_type)}`}
                value={form.document_type}
                onChange={(e) => updateField("document_type", e.target.value)}
                required
              >
                <option value="">{t("select")}</option>
                <option value="identity_card">{t("docIdentityCard")}</option>
                <option value="passport">{t("docPassport")}</option>
              </select>
            </div>
            <div className="sm:col-span-2">
              <label className="label" htmlFor="document_number">
                {t("documentNumber")}
              </label>
              <input
                id="document_number"
                className={`input mt-1 ${confidenceClass(fieldConfidence.document_number)}`}
                value={form.document_number}
                onChange={(e) => updateField("document_number", e.target.value)}
                required
              />
              {confidenceHint("document_number") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("document_number")}</p>
              ) : null}
            </div>
            <div className="sm:col-span-2">
              <label className="label" htmlFor="address">
                {t("address")}
              </label>
              <p className="mt-1 text-xs text-muted">{t("addressHint")}</p>
              <textarea
                id="address"
                className={`input mt-1 min-h-20 ${confidenceClass(fieldConfidence.address)}`}
                value={form.address}
                placeholder={t("addressExample")}
                onChange={(e) => updateField("address", e.target.value)}
                required
              />
              <p className="mt-1 text-xs text-muted">
                {t("addressExampleLabel")}: {t("addressExample")}
              </p>
              {confidenceHint("address") ? (
                <p className="mt-1 text-xs text-amber-700">{confidenceHint("address")}</p>
              ) : null}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 pt-2">
            {step > 0 ? (
              <button
                type="button"
                className="btn-ghost"
                onClick={() => {
                  const prev = step - 1;
                  setStep(prev);
                  resetSlotUi(session.slots[prev]);
                }}
              >
                {t("back")}
              </button>
            ) : entryMode === "form" ? (
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setEntryMode("choose")}
              >
                {t("back")}
              </button>
            ) : null}
            {step < session.slots.length - 1 ? (
              <button type="submit" className="btn" disabled={!canProceed || saving}>
                {t("next")}
              </button>
            ) : session.can_complete ? (
              <button
                type="button"
                className="btn"
                disabled={completing || saving}
                onClick={() => void onComplete()}
              >
                {completing ? t("completing") : t("finish")}
              </button>
            ) : (
              <button type="submit" className="btn" disabled={!canProceed || saving}>
                {t("save")}
              </button>
            )}
          </div>
        </form>
      ) : null}

      {session.can_complete && step < session.slots.length - 1 ? (
        <div className="card border-stay-blue bg-stay-blue-light/40">
          <p className="text-sm text-stay-navy">{t("allReadyHint")}</p>
          <button
            type="button"
            className="btn mt-3"
            disabled={completing}
            onClick={() => void onComplete()}
          >
            {completing ? t("completing") : t("finish")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
