"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useMessageTranslateCache } from "@/app/_components/MessageTranslateCacheProvider";
import { LinkifiedText } from "@/lib/linkifyText";
import { handlePreviewUrlClick } from "@/lib/messageInbox";
import {
  buildTranslatePayload,
  buildTranslateUrl,
  canTranslateBody,
  isStaleTranslateResponse,
  isTranslateAbortError,
  parseTranslateResponse,
  translateCacheKey,
  translateDisplayState,
  type TranslateResult,
  type TranslateViewMode,
} from "@/lib/messageTranslate";
import type { GuestMessageTimelineItem } from "@/lib/types";

type Props = {
  reservationId: number;
  item: GuestMessageTimelineItem;
  className?: string;
  linkClassName?: string;
  controlClassName?: string;
};

export function MessageBodyWithTranslate({
  reservationId,
  item,
  className,
  linkClassName,
  controlClassName = "text-xs font-medium text-stay-blue hover:underline",
}: Props) {
  const locale = useLocale();
  const t = useTranslations("guestMessages");
  const cache = useMessageTranslateCache();
  const key = translateCacheKey({ reservationId, timelineId: item.id, lang: locale });
  const localeRef = useRef(locale);
  localeRef.current = locale;

  const [result, setResult] = useState<TranslateResult | undefined>(() => cache.getOk(key));
  const [viewMode, setViewMode] = useState<TranslateViewMode>(() => cache.getViewMode(key));
  const [busy, setBusy] = useState(() => Boolean(cache.getPending(key)));
  const [error, setError] = useState("");

  useEffect(() => {
    const pending = cache.getPending(key);
    setResult(cache.getOk(key));
    setViewMode(cache.getViewMode(key));
    setError("");
    if (!pending) {
      setBusy(false);
      return;
    }

    let ignore = false;
    setBusy(true);
    void pending.promise
      .then((data) => {
        if (
          ignore ||
          isStaleTranslateResponse({
            requestLang: locale,
            activeLang: localeRef.current,
            unmounted: ignore,
          })
        ) {
          return;
        }
        setResult(cache.getOk(key) ?? data);
        setViewMode(cache.getViewMode(key));
        setError("");
      })
      .catch((err: unknown) => {
        if (ignore || isTranslateAbortError(err)) return;
        if (
          isStaleTranslateResponse({
            requestLang: locale,
            activeLang: localeRef.current,
            unmounted: ignore,
          })
        ) {
          return;
        }
        setError(t("translateFailed"));
      })
      .finally(() => {
        if (!ignore) setBusy(false);
      });

    return () => {
      ignore = true;
    };
  }, [cache, key, locale, t]);

  const display = translateDisplayState(result);
  const showTranslated = display.canShowTranslation && viewMode === "translated";
  const text = showTranslated && result ? result.translated : item.body_text;
  const showTranslateAction =
    canTranslateBody(item.body_text) && !display.hideChrome && !result && !error;

  async function requestTranslate(event: MouseEvent<HTMLButtonElement>) {
    handlePreviewUrlClick(event);
    if (busy) return;

    const requestLang = locale;
    const requestKey = translateCacheKey({
      reservationId,
      timelineId: item.id,
      lang: requestLang,
    });
    setError("");
    setBusy(true);
    try {
      const data = await cache.getOrCreate(requestKey, async (signal) => {
        const res = await fetch(buildTranslateUrl(reservationId), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(buildTranslatePayload(item, requestLang)),
          signal,
        });
        if (!res.ok) throw new Error(t("translateFailed"));
        return parseTranslateResponse(await res.json());
      });
      if (
        isStaleTranslateResponse({
          requestLang,
          activeLang: localeRef.current,
          unmounted: false,
        })
      ) {
        return;
      }
      setResult(cache.getOk(requestKey) ?? data);
      setViewMode(cache.getViewMode(requestKey));
    } catch (err: unknown) {
      if (isTranslateAbortError(err)) return;
      if (
        isStaleTranslateResponse({
          requestLang,
          activeLang: localeRef.current,
          unmounted: false,
        })
      ) {
        return;
      }
      setError(t("translateFailed"));
    } finally {
      setBusy(false);
    }
  }

  function toggleView(event: MouseEvent<HTMLButtonElement>) {
    handlePreviewUrlClick(event);
    const next: TranslateViewMode = viewMode === "translated" ? "original" : "translated";
    cache.setViewMode(key, next);
    setViewMode(next);
  }

  return (
    <div className="space-y-1">
      <LinkifiedText className={className} linkClassName={linkClassName}>
        {text}
      </LinkifiedText>
      {showTranslateAction ? (
        <button
          type="button"
          className={controlClassName}
          disabled={busy}
          aria-busy={busy}
          onClick={(event) => void requestTranslate(event)}
        >
          {t("translateAction")}
        </button>
      ) : null}
      {display.canShowTranslation ? (
        <>
          <button type="button" className={controlClassName} onClick={toggleView}>
            {viewMode === "translated" ? t("showOriginal") : t("showTranslation")}
          </button>
          {viewMode === "translated" ? (
            <p className="text-xs opacity-80">{t("translatedLabel")}</p>
          ) : null}
        </>
      ) : null}
      {error ? (
        <div className="space-y-1">
          <p className="text-xs text-red-600">{error}</p>
          <button
            type="button"
            className={controlClassName}
            disabled={busy}
            aria-busy={busy}
            onClick={(event) => void requestTranslate(event)}
          >
            {t("retry")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
