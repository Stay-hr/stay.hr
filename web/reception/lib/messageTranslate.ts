export type TranslateResult = {
  timeline_id: number;
  original: string;
  translated: string;
  target_lang: string;
  is_translated: boolean;
  from_cache: boolean;
};

export type TranslateViewMode = "original" | "translated";

export type TranslateCacheKeyInput = {
  reservationId: number;
  timelineId: number;
  lang: string;
};

export type TranslateCacheEntry =
  | {
      status: "pending";
      requestId: number;
      promise: Promise<TranslateResult>;
      abort: AbortController;
    }
  | { status: "ok"; result: TranslateResult };

export function translateCacheKey({
  reservationId,
  timelineId,
  lang,
}: TranslateCacheKeyInput): string {
  return `${reservationId}:${timelineId}:${lang}`;
}

export function viewModeStorageKey(input: TranslateCacheKeyInput): string {
  return translateCacheKey(input);
}

export function buildTranslateUrl(reservationId: number): string {
  return `/api/stay/reception/reservations/${reservationId}/messages/translate/`;
}

export function buildTranslatePayload(
  item: { id: number },
  lang: string,
): { timeline_id: number; lang: string } {
  return { timeline_id: item.id, lang };
}

export function canTranslateBody(text: string | null | undefined): boolean {
  return Boolean((text ?? "").trim());
}

export function translateDisplayState(result: TranslateResult | null | undefined): {
  hideChrome: boolean;
  canShowTranslation: boolean;
} {
  if (!result) return { hideChrome: false, canShowTranslation: false };
  if (!result.is_translated) return { hideChrome: true, canShowTranslation: false };
  return { hideChrome: false, canShowTranslation: true };
}

export function isStaleTranslateResponse(opts: {
  requestLang: string;
  activeLang: string;
  unmounted: boolean;
}): boolean {
  return opts.unmounted || opts.requestLang !== opts.activeLang;
}

export function shouldClearPendingEntry(
  entry: TranslateCacheEntry | undefined,
  requestId: number,
): boolean {
  return entry?.status === "pending" && entry.requestId === requestId;
}

export function isTranslateAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

export function parseTranslateResponse(data: unknown): TranslateResult {
  if (!data || typeof data !== "object") {
    throw new Error("invalid_translate_response");
  }
  const row = data as Record<string, unknown>;
  return {
    timeline_id: Number(row.timeline_id),
    original: String(row.original ?? ""),
    translated: String(row.translated ?? ""),
    target_lang: String(row.target_lang ?? ""),
    is_translated: Boolean(row.is_translated),
    from_cache: Boolean(row.from_cache),
  };
}

export class MessageTranslateSession {
  private readonly results = new Map<string, TranslateCacheEntry>();
  private readonly viewModes = new Map<string, TranslateViewMode>();
  private readonly aborts = new Set<AbortController>();
  private nextRequestId = 1;

  getOrCreate(
    key: string,
    start: (signal: AbortSignal) => Promise<TranslateResult>,
  ): Promise<TranslateResult> {
    const existing = this.results.get(key);
    if (existing?.status === "ok") return Promise.resolve(existing.result);
    if (existing?.status === "pending") return existing.promise;

    const requestId = this.nextRequestId++;
    const abort = new AbortController();
    this.aborts.add(abort);

    const promise = (async () => {
      try {
        const result = await start(abort.signal);
        const current = this.results.get(key);
        if (current?.status === "pending" && current.requestId === requestId) {
          this.results.set(key, { status: "ok", result });
          if (result.is_translated && !this.viewModes.has(key)) {
            this.viewModes.set(key, "translated");
          }
        }
        return result;
      } catch (err) {
        const current = this.results.get(key);
        if (shouldClearPendingEntry(current, requestId)) {
          this.results.delete(key);
        }
        throw err;
      } finally {
        this.aborts.delete(abort);
      }
    })();

    this.results.set(key, { status: "pending", requestId, promise, abort });
    return promise;
  }

  getOk(key: string): TranslateResult | undefined {
    const entry = this.results.get(key);
    return entry?.status === "ok" ? entry.result : undefined;
  }

  getPending(key: string): Extract<TranslateCacheEntry, { status: "pending" }> | undefined {
    const entry = this.results.get(key);
    return entry?.status === "pending" ? entry : undefined;
  }

  getViewMode(key: string): TranslateViewMode {
    const stored = this.viewModes.get(key);
    if (stored) return stored;
    return this.getOk(key)?.is_translated ? "translated" : "original";
  }

  setViewMode(key: string, mode: TranslateViewMode): void {
    this.viewModes.set(key, mode);
  }

  abortAll(): void {
    for (const controller of this.aborts) {
      controller.abort();
    }
    this.aborts.clear();
  }
}
