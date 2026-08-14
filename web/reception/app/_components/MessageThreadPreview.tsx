"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { LinkifiedText } from "@/lib/linkifyText";
import {
  handlePreviewUrlClick,
  previewClampClass,
  previewOverflows,
  shouldShowPreviewToggle,
} from "@/lib/messageInbox";

type Props = {
  preview: string;
  expanded: boolean;
  onToggle: () => void;
};

function ChevronIcon({ up }: { up: boolean }) {
  return (
    <svg
      viewBox="0 0 20 20"
      aria-hidden="true"
      className={`h-4 w-4 ${up ? "rotate-180" : ""}`}
    >
      <path
        fill="currentColor"
        d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06z"
      />
    </svg>
  );
}

export function MessageThreadPreview({ preview, expanded, onToggle }: Props) {
  const t = useTranslations("messageInbox");
  const measureRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = measureRef.current;
    if (!el) return;

    const measure = () => {
      if (expanded) return;
      setOverflows(previewOverflows(el));
    };

    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [expanded, preview]);

  const showToggle = shouldShowPreviewToggle({ overflows, expanded });
  const label = expanded ? t("collapsePreview") : t("expandPreview");

  return (
    <div className="flex items-start gap-1 border-t border-stay-border px-3 py-2">
      <div
        ref={measureRef}
        className={`min-w-0 flex-1 text-sm text-muted ${previewClampClass(expanded)}`}
      >
        <LinkifiedText className="whitespace-pre-wrap text-sm text-muted">{preview}</LinkifiedText>
      </div>
      {showToggle ? (
        <button
          type="button"
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-stay-muted outline-none transition hover:bg-stay-blue-light hover:text-stay-blue focus-visible:ring-2 focus-visible:ring-stay-blue"
          aria-expanded={expanded}
          aria-label={label}
          title={label}
          onClick={(event) => {
            handlePreviewUrlClick(event);
            onToggle();
          }}
        >
          <ChevronIcon up={expanded} />
        </button>
      ) : null}
    </div>
  );
}
