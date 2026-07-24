import { Suspense, type ReactNode } from "react";
import { SettingsShell } from "@/app/settings/_components/SettingsShell";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<div className="min-h-screen bg-stay-surface" />}>
      <SettingsShell>{children}</SettingsShell>
    </Suspense>
  );
}
