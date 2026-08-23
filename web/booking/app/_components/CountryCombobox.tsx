"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useLocale } from "next-intl";
import {
  COUNTRIES,
  countryByIso2,
  countryLabel,
  filterCountries,
  type CountryRecord,
} from "@/lib/countries";

type Props = {
  id?: string;
  value: string;
  onChange: (iso2: string) => void;
  required?: boolean;
  className?: string;
  placeholder?: string;
  errorMessage?: string;
};

const MAX_VISIBLE = 12;

export function CountryCombobox({
  id,
  value,
  onChange,
  required = false,
  className = "",
  placeholder = "",
  errorMessage,
}: Props) {
  const locale = useLocale();
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const selected = useMemo(() => countryByIso2(value), [value]);

  const results = useMemo(() => {
    const filtered = filterCountries(query, locale);
    return filtered.slice(0, MAX_VISIBLE);
  }, [query, locale]);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query, open]);

  const displayValue = open
    ? query
    : selected
      ? countryLabel(selected, locale)
      : "";

  function selectCountry(country: CountryRecord) {
    onChange(country.iso2);
    setQuery("");
    setOpen(false);
  }

  function onInputChange(next: string) {
    setQuery(next);
    setOpen(true);
    if (!next.trim()) {
      onChange("");
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!open && (event.key === "ArrowDown" || event.key === "Enter")) {
      setOpen(true);
      return;
    }
    if (!open) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((idx) => Math.min(idx + 1, Math.max(results.length - 1, 0)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((idx) => Math.max(idx - 1, 0));
    } else if (event.key === "Enter" && results[activeIndex]) {
      event.preventDefault();
      selectCountry(results[activeIndex]);
    } else if (event.key === "Escape") {
      setOpen(false);
      setQuery("");
    }
  }

  return (
    <div ref={rootRef} className="relative">
      <input
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        className={`input mt-1 ${className}`}
        value={displayValue}
        placeholder={placeholder}
        required={required}
        autoComplete="off"
        onFocus={() => {
          setOpen(true);
          if (selected && !query) {
            setQuery("");
          }
        }}
        onChange={(e) => onInputChange(e.target.value)}
        onKeyDown={onKeyDown}
      />
      {open && results.length > 0 ? (
        <ul
          id={listId}
          role="listbox"
          className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded-md border border-slate-200 bg-white shadow-lg"
        >
          {results.map((country, index) => (
            <li
              key={country.iso2}
              role="option"
              aria-selected={country.iso2 === value}
              className={`cursor-pointer px-3 py-2 text-sm ${
                index === activeIndex ? "bg-slate-100" : "hover:bg-slate-50"
              }`}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => selectCountry(country)}
              onMouseEnter={() => setActiveIndex(index)}
            >
              <span className="font-medium">{countryLabel(country, locale)}</span>
              <span className="ml-2 text-xs text-slate-500">{country.iso2}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {errorMessage ? (
        <p className="mt-1 text-xs text-red-700">{errorMessage}</p>
      ) : null}
      <input type="hidden" name={`${id || "nationality"}_iso2`} value={value} readOnly />
    </div>
  );
}

export { COUNTRIES };
