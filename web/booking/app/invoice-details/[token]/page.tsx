import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { GuestInvoiceDetailsView } from "@/app/_components/GuestInvoiceDetailsView";
import { BookingShell } from "@/app/_components/BookingShell";
import { getSiteContext, propertyBasePath, resolvePropertySlug } from "@/lib/site-context";
import { requestHost } from "@/lib/utils";

type Props = {
  params: Promise<{ token: string }>;
  searchParams: Promise<{ lang?: string }>;
};

export default async function GuestInvoiceDetailsPage({ params, searchParams }: Props) {
  const { token } = await params;
  const { lang } = await searchParams;
  const host = await requestHost();
  let ctx;
  try {
    ctx = await getSiteContext(host);
  } catch {
    notFound();
  }

  const propertySlug = resolvePropertySlug(ctx, null);
  if (!propertySlug) {
    notFound();
  }

  propertyBasePath(ctx, propertySlug);
  await getTranslations("guestInvoiceDetails");

  return (
    <BookingShell ctx={ctx} propertySlug={propertySlug}>
      <GuestInvoiceDetailsView token={decodeURIComponent(token)} lang={lang ?? null} />
    </BookingShell>
  );
}
