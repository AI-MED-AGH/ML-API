export type ModelInfo = { name: string; state: string; version: string | null };
export type Schema = { model_version?: string; request_schema?: unknown; response_schema?: unknown; example?: unknown };

export type Reply = {
  status: number;
  ok: boolean;
  ms: number;
  headers: Record<string, string>;
  body: unknown;
  raw: string;
};

const INTERESTING = ["x-model-status", "retry-after", "content-type"];

export async function call(key: string, method: string, path: string, body?: unknown): Promise<Reply> {
  const started = performance.now();
  try {
    const res = await fetch(`/api${path}`, {
      method,
      headers: { "X-API-Key": key, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const raw = await res.text();
    let parsed: unknown = raw;
    try {
      parsed = raw ? JSON.parse(raw) : null;
    } catch {
      /* not JSON */
    }
    const headers: Record<string, string> = {};
    res.headers.forEach((v, k) => INTERESTING.includes(k) && (headers[k] = v));
    return { status: res.status, ok: res.ok, ms: Math.round(performance.now() - started), headers, body: parsed, raw };
  } catch (err) {
    return {
      status: 0, ok: false, ms: Math.round(performance.now() - started), headers: {},
      body: { error: "Network error", detail: String(err) }, raw: "",
    };
  }
}

export const errorType = (r: Reply): string | undefined =>
  typeof r.body === "object" && r.body !== null ? ((r.body as Record<string, unknown>).error_type as string | undefined) : undefined;
