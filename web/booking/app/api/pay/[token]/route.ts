import { headers } from "next/headers";
import { NextResponse } from "next/server";

type Params = { params: Promise<{ token: string }> };

function internalApiBase(): string {
  return (process.env.STAY_API_INTERNAL_URL || "http://stay-django:8000").replace(/\/+$/, "");
}

async function hostHeader(): Promise<string> {
  const h = await headers();
  return (h.get("x-forwarded-host") || h.get("host") || "").split(":")[0];
}

async function proxyGuestPayment(path: string, init?: RequestInit): Promise<NextResponse> {
  const host = await hostHeader();
  if (!host) {
    return NextResponse.json({ error: "Missing host" }, { status: 400 });
  }

  const url = `${internalApiBase()}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: {
      Accept: "application/json",
      "X-Forwarded-Host": host.split(":")[0].toLowerCase(),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
    cache: "no-store",
  });

  const text = await res.text();
  let data: unknown = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 200) };
    }
  }

  return NextResponse.json(data, { status: res.status });
}

export async function GET(request: Request, { params }: Params) {
  const { token } = await params;
  const { searchParams } = new URL(request.url);
  const lang = searchParams.get("lang");
  const qs = lang ? `?lang=${encodeURIComponent(lang)}` : "";
  return proxyGuestPayment(`/api/v1/public/payment/${encodeURIComponent(token)}/${qs}`);
}
