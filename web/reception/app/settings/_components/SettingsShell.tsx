"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { ReceptionNav } from "@/app/_components/ReceptionNav";
import { SettingsPropertyPicker } from "@/app/settings/_components/SettingsPropertyPicker";
import { SettingsSubNav } from "@/app/settings/_components/SettingsSubNav";
import {
  PROPERTY_SETTINGS_STORAGE_KEY,
  firstEnabledSettingsTab,
  settingsSurfaceEnabled,
  type PropertySettingsRoot,
  type ReceptionPropertySummary,
} from "@/lib/propertySettings";

type SettingsContextValue = {
  root: PropertySettingsRoot | null;
  properties: ReceptionPropertySummary[];
  propertyId: number | null;
  setPropertyId: (id: number) => void;
  loading: boolean;
  error: string;
};

const SettingsContext = createContext<SettingsContextValue | null>(null);

export function useSettingsShell(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) {
    throw new Error("useSettingsShell must be used within SettingsShell");
  }
  return ctx;
}

function readStoredPropertyId(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(PROPERTY_SETTINGS_STORAGE_KEY);
    if (!raw) return null;
    const id = Number(raw);
    return Number.isFinite(id) ? id : null;
  } catch {
    return null;
  }
}

function writeStoredPropertyId(id: number) {
  try {
    window.localStorage.setItem(PROPERTY_SETTINGS_STORAGE_KEY, String(id));
  } catch {
    // ignore quota / private mode
  }
}

export function SettingsShell({ children }: { children: ReactNode }) {
  const t = useTranslations("settings");
  const tc = useTranslations("common");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const [tenantName, setTenantName] = useState("");
  const [root, setRoot] = useState<PropertySettingsRoot | null>(null);
  const [properties, setProperties] = useState<ReceptionPropertySummary[]>([]);
  const [propertyId, setPropertyIdState] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const syncPropertyQuery = useCallback(
    (id: number | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (id != null) {
        params.set("property", String(id));
      } else {
        params.delete("property");
      }
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  const setPropertyId = useCallback(
    (id: number) => {
      setPropertyIdState(id);
      writeStoredPropertyId(id);
      syncPropertyQuery(id);
    },
    [syncPropertyQuery],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      setError("");
      try {
        const session = await fetch("/api/auth/session");
        if (session.ok) {
          const data = await session.json();
          if (!cancelled) setTenantName(data.tenant || "");
        }

        const [settingsRes, propertiesRes] = await Promise.all([
          fetch("/api/stay/reception/settings/"),
          fetch("/api/stay/reception/properties/"),
        ]);

        if (!settingsRes.ok) {
          throw new Error(t("loadFailed"));
        }
        const settingsJson = (await settingsRes.json()) as PropertySettingsRoot;
        if (cancelled) return;
        setRoot(settingsJson);

        if (!settingsSurfaceEnabled(settingsJson)) {
          setProperties([]);
          setPropertyIdState(null);
          return;
        }

        if (!propertiesRes.ok) {
          throw new Error(t("loadFailed"));
        }
        const propertiesJson = (await propertiesRes.json()) as {
          results?: ReceptionPropertySummary[];
        };
        const nextProperties = propertiesJson.results ?? [];
        setProperties(nextProperties);

        const fromQuery = Number(searchParams.get("property") || "");
        const stored = readStoredPropertyId();
        const preferred =
          (Number.isFinite(fromQuery) && nextProperties.some((p) => p.id === fromQuery)
            ? fromQuery
            : null) ??
          (stored != null && nextProperties.some((p) => p.id === stored) ? stored : null) ??
          nextProperties[0]?.id ??
          null;

        setPropertyIdState(preferred);
        if (preferred != null) {
          writeStoredPropertyId(preferred);
          if (!Number.isFinite(fromQuery) || fromQuery !== preferred) {
            const params = new URLSearchParams(searchParams.toString());
            params.set("property", String(preferred));
            const query = params.toString();
            router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
          }
        }
      } catch {
        if (!cancelled) setError(t("loadFailed"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Bootstrap once on mount; property query sync is handled separately.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional mount bootstrap
  }, []);

  const value = useMemo<SettingsContextValue>(
    () => ({
      root,
      properties,
      propertyId,
      setPropertyId,
      loading,
      error,
    }),
    [root, properties, propertyId, setPropertyId, loading, error],
  );

  const enabled = settingsSurfaceEnabled(root);
  const firstTab = firstEnabledSettingsTab(root?.tabs);

  return (
    <SettingsContext.Provider value={value}>
      <div className="min-h-screen bg-stay-surface">
        <ReceptionNav tenantName={tenantName} />
        <main className="mx-auto max-w-6xl px-4 py-6">
          <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
            <h1 className="text-2xl font-bold text-stay-navy">{t("title")}</h1>
            {enabled && !loading ? (
              <SettingsPropertyPicker
                properties={properties}
                propertyId={propertyId}
                onChange={setPropertyId}
              />
            ) : null}
          </div>

          {loading ? <p className="text-stay-muted">{tc("loading")}</p> : null}
          {error ? <p className="text-sm text-red-600">{error}</p> : null}

          {!loading && !error && !enabled ? (
            <p className="text-sm text-stay-muted">{t("disabled")}</p>
          ) : null}

          {!loading && enabled && root ? (
            <>
              <SettingsSubNav tabs={root.tabs} propertyId={propertyId} />
              {children}
              {!firstTab ? <p className="text-sm text-stay-muted">{t("noTabs")}</p> : null}
            </>
          ) : null}
        </main>
      </div>
    </SettingsContext.Provider>
  );
}
