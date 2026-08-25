type ApiErrorBody = {
  detail?: unknown;
  reply?: unknown;
};

function firstString(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (Array.isArray(value) && typeof value[0] === "string" && value[0].trim()) {
    return value[0];
  }
  return undefined;
}

export async function extractApiError(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null);
  if (typeof body === "string") {
    return firstString(body) ?? fallback;
  }
  if (Array.isArray(body)) {
    return firstString(body) ?? fallback;
  }
  if (body && typeof body === "object") {
    const rec = body as ApiErrorBody;
    return firstString(rec.reply) ?? firstString(rec.detail) ?? fallback;
  }
  return fallback;
}
