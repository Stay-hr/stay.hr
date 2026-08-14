import type { MouseEvent, ReactNode } from "react";
import { handlePreviewUrlClick } from "@/lib/messageInbox";

const URL_REGEX = /https?:\/\/[^\s]+/g;

function splitUrlAndTrailingPunct(url: string): { href: string; trailing: string } {
  const match = url.match(/^(https?:\/\/[^\s]*?)([.,]+)$/);
  if (match) {
    return { href: match[1], trailing: match[2] };
  }
  return { href: url, trailing: "" };
}

export function extractHttpUrls(text: string): string[] {
  const hrefs: string[] = [];
  for (const match of text.matchAll(URL_REGEX)) {
    hrefs.push(splitUrlAndTrailingPunct(match[0]).href);
  }
  return hrefs;
}

export function linkifiedAnchorProps(href: string): {
  href: string;
  target: "_blank";
  rel: "noopener noreferrer";
  "data-message-preview-url": "";
} {
  return {
    href,
    target: "_blank",
    rel: "noopener noreferrer",
    "data-message-preview-url": "",
  };
}

type LinkifiedTextProps = {
  children: string;
  className?: string;
  linkClassName?: string;
};

export function LinkifiedText({ children, className, linkClassName }: LinkifiedTextProps) {
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let key = 0;

  for (const match of children.matchAll(URL_REGEX)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      nodes.push(children.slice(lastIndex, index));
    }
    const raw = match[0];
    const { href, trailing } = splitUrlAndTrailingPunct(raw);
    nodes.push(
      <a
        key={key++}
        {...linkifiedAnchorProps(href)}
        className={linkClassName ?? "text-stay-blue underline"}
        onClick={(event: MouseEvent<HTMLAnchorElement>) => {
          handlePreviewUrlClick(event);
        }}
      >
        {href}
      </a>,
    );
    if (trailing) {
      nodes.push(trailing);
    }
    lastIndex = index + raw.length;
  }

  if (lastIndex < children.length) {
    nodes.push(children.slice(lastIndex));
  }

  return <p className={className}>{nodes.length ? nodes : children}</p>;
}
