import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { buildDjangoApiPath } from "@/lib/django-api-path";
import { buildStayAuthHeaders } from "@/lib/stay-server";

type RouteContext = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, pathSegments: string[]) {
  const authHeaders = await buildStayAuthHeaders();
  if (!authHeaders) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const url = new URL(request.url);
  const query = url.search;
  const path = buildDjangoApiPath(pathSegments);
  const relativePath = pathSegments.filter(Boolean).join("/");
  const isSsePath = relativePath.includes("reservation-versions/stream");

  // SSE: wire client AbortSignal into upstream fetch so EventSource.close()/tab close
  // cancels Django and frees the Gunicorn worker (ADR 0005 Phase 1 lifecycle).
  // Permanent: keep AbortSignal + bff_sse_* logs through Redis (2a) and Uvicorn (2b).
  if (isSsePath) {
    const abortedAtStart = request.signal.aborted;
    console.info(
      JSON.stringify({
        event: "bff_sse_proxy_start",
        path: relativePath,
        query,
        signal_aborted: abortedAtStart,
        upstream_abort_wired: true,
      }),
    );
    request.signal.addEventListener(
      "abort",
      () => {
        console.info(
          JSON.stringify({
            event: "bff_sse_client_aborted",
            path: relativePath,
            query,
            upstream_abort_wired: true,
            note: "client gone; upstream fetch aborted by BFF",
          }),
        );
      },
      { once: true },
    );
  }

  const headers: Record<string, string> = { ...authHeaders };

  // Optimistic locking for property settings (and any other callers).
  const ifMatch = request.headers.get("if-match");
  if (ifMatch) {
    headers["If-Match"] = ifMatch;
  }
  const ifNoneMatch = request.headers.get("if-none-match");
  if (ifNoneMatch) {
    headers["If-None-Match"] = ifNoneMatch;
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    cache: "no-store",
    redirect: "manual",
  };

  if (isSsePath) {
    init.signal = request.signal;
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    const contentType = request.headers.get("content-type") || "";
    if (contentType.includes("multipart/form-data")) {
      init.body = await request.arrayBuffer();
      headers["Content-Type"] = contentType;
    } else {
      const body = await request.text();
      if (body) {
        headers["Content-Type"] = contentType || "application/json";
        init.body = body;
      }
    }
  }

  const internal = (process.env.STAY_API_INTERNAL_URL || "http://stay_django:8000").replace(
    /\/+$/,
    "",
  );
  const upstream = await fetch(`${internal}${path}${query}`, init);

  if (upstream.status >= 300 && upstream.status < 400) {
    return NextResponse.json(
      { detail: "Unexpected redirect from Stay API. Check trailing slash on upstream path." },
      { status: 502 },
    );
  }

  const contentType = upstream.headers.get("content-type") || "application/json";
  const isEventStream = contentType.includes("text/event-stream");

  if (isEventStream && upstream.body) {
    const streamId = upstream.headers.get("x-sse-stream-id");
    const responseHeaders: Record<string, string> = {
      "Content-Type": "text/event-stream",
      "Cache-Control": upstream.headers.get("cache-control") || "no-cache",
      Connection: "keep-alive",
    };
    const buffering = upstream.headers.get("x-accel-buffering");
    if (buffering) {
      responseHeaders["X-Accel-Buffering"] = buffering;
    }
    if (streamId) {
      responseHeaders["X-SSE-Stream-Id"] = streamId;
    }
    console.info(
      JSON.stringify({
        event: "bff_sse_upstream_connected",
        path: relativePath,
        query,
        stream_id: streamId,
        upstream_abort_wired: true,
      }),
    );
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  }

  const isBinary =
    contentType.startsWith("image/") ||
    contentType.includes("octet-stream") ||
    contentType.startsWith("application/pdf");

  const body = isBinary ? await upstream.arrayBuffer() : await upstream.text();

  const responseHeaders: Record<string, string> = {
    "Content-Type": contentType,
  };
  const etag = upstream.headers.get("etag");
  if (etag) {
    responseHeaders["ETag"] = etag;
  }
  if (relativePath.includes("reception/reviews")) {
    responseHeaders["Cache-Control"] = "no-store, no-cache, must-revalidate";
    responseHeaders["Pragma"] = "no-cache";
  }
  if (isBinary && upstream.headers.get("cache-control")) {
    responseHeaders["Cache-Control"] = upstream.headers.get("cache-control")!;
  }

  return new NextResponse(body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export async function GET(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function POST(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function PUT(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}
