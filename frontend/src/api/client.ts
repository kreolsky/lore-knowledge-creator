/** Thin HTTP client wrapping fetch with credentials, typed HTTP errors, retry, and AbortSignal. */
// SYSTEM: api-client — HTTP client with cookie auth, retry

const BASE_HEADERS = { 'Content-Type': 'application/json' };
const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 1000;

export class AuthError extends Error {
  constructor() { super('Authentication required'); this.name = 'AuthError'; }
}

export class ForbiddenError extends Error {
  /** The 403 body's `detail` (a string or a `{code, …}` object); undefined on a bare body. */
  detail: unknown;
  constructor(detail?: unknown) { super('Access forbidden'); this.name = 'ForbiddenError'; this.detail = detail; }
}

/**
 * Did the server REFUSE this resource, as opposed to failing to serve it?
 *
 * A revoked reader is answered 404, not 403, on every READ path — measured on both
 * `/api/projects/{id}` and `/api/documents/open/{id}` — because a 403 would confirm the
 * resource exists. 403 belongs to write and ownership checks (`backend/access.py`), which
 * no read handler reaches; it is covered here anyway because ForbiddenError carries no
 * `status` field, so a caller testing `err.status === 403` fails silently instead of
 * loudly. Callers get one predicate rather than each spelling the status pair themselves.
 */
export function isAccessRefusal(err: unknown): boolean {
  if (err instanceof ForbiddenError) return true;
  return (err as { status?: unknown } | null)?.status === 404;
}

export class ServiceUnavailableError extends Error {
  constructor(status: number) { super(`Server unavailable (${status})`); this.name = 'ServiceUnavailableError'; }
}

export class HttpError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail?: string) {
    super(`HTTP ${status}${detail ? `: ${detail}` : ''}`);
    this.name = 'HttpError';
    this.status = status;
    this.detail = detail ?? '';
  }
}

/**
 * Thrown when a chat-completions request body exceeds the server cap (413).
 * Carries the numeric `limitMb` from the structured 413 body so callers can show
 * a localized "request too large" message instead of a silent failure.
 */
export class RequestTooLargeError extends Error {
  limitMb?: number;
  constructor(limitMb?: number) {
    super('Request body too large');
    this.name = 'RequestTooLargeError';
    this.limitMb = limitMb;
  }
}

/**
 * INVARIANT: redirecting guard prevents multiple concurrent 401 redirects.
 * Reset after navigation so subsequent 401s (e.g. after re-login) still redirect.  Why: without the guard two concurrent 401s fire two redirects; resetting after nav lets a later 401 (post re-login) redirect again.
 */
export let redirecting = false;
export function _resetRedirectGuard() { redirecting = false; }
window.addEventListener('pageshow', _resetRedirectGuard);

interface RequestOptions {
  signal?: AbortSignal;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- system boundary: callers rely on untyped JSON
async function handleResponse(res: Response): Promise<any> {
  if (res.status === 401) {
    if (!redirecting) { redirecting = true; window.location.href = '/'; }
    throw new AuthError();
  }
  if (res.status === 403) {
    // WHY: a structured 403 (`{code: 'model_forbidden', model}` from the chat
    // turn) names WHICH refusal fired — the caller's toast hangs on it.
    let detail: unknown;
    try { detail = (await res.clone().json())?.detail; } catch { /* bare body */ }
    throw new ForbiddenError(detail);
  }
  if (res.status === 502 || res.status === 503) throw new ServiceUnavailableError(res.status);
  if (res.status === 413) {
    // WHY: the chat-completions body cap answers 413 with a structured
    // {limit_mb} body — the specific toast (chatRequestTooLarge) hangs on the
    // typed error. Lives here because the chat transport is a plain POST
    // through handleResponse.
    let limitMb: number | undefined;
    try { limitMb = (await res.clone().json())?.limit_mb; } catch { /* bare body */ }
    throw new RequestTooLargeError(limitMb);
  }
  if (!res.ok) throw new HttpError(res.status, await res.text().catch(() => ''));
  if (res.status === 204) return undefined;
  return res.json();
}

function isRetryable(err: unknown): boolean {
  return err instanceof ServiceUnavailableError;
}

async function fetchWithRetry(
  input: string, init: RequestInit, opts?: RequestOptions,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any> {
  const fetchOpts: RequestInit = { ...init, credentials: 'include' };
  if (opts?.signal) fetchOpts.signal = opts.signal;

  let lastErr: unknown;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const res = await fetch(input, fetchOpts);
      return await handleResponse(res);
    } catch (err) {
      lastErr = err;
      if (!isRetryable(err) || attempt === MAX_RETRIES) throw err;
      // WHY: exponential backoff (1s, 2s) for transient 502/503 from load balancer or DB restart
      await new Promise(r => setTimeout(r, RETRY_BASE_DELAY_MS * (attempt + 1)));
    }
  }
  throw lastErr;
}

export const apiClient = {
  async get(endpoint: string, opts?: RequestOptions) {
    return fetchWithRetry(`/api${endpoint}`, {}, opts);
  },
  /**
   * Non-redirecting status probe (plan "public-document-ids").
   *
   * ARCH: unlike get(), a 401 does NOT redirect to login — it resolves to
   * { ok: false, status: 401 } so the /docs/:id decider can fall through to the
   * anonymous public branch. The decider is the ONLY caller: every other authed
   * request must keep the redirect-on-401 guard. Returns parsed JSON on success.
   * No retry (a probe is one-shot by design).
   *
   * ARCH (plan "post-public-document-ids-fixes"): accepts an optional AbortSignal
   * so the decider can bound the probe with a timeout. On abort, fetch rejects with
   * an AbortError (NOT a { ok:false } resolution) — the caller's .catch decides.
   * Why: a hung /auth/me must not leave the decider on an infinite spinner; the
   * decider turns a timeout/abort into its explicit error phase.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- system boundary: callers cast
  async probe(endpoint: string, signal?: AbortSignal): Promise<{ ok: boolean; status: number; data?: any }> {
    const res = await fetch(`/api${endpoint}`, { credentials: 'include', signal });
    if (!res.ok) return { ok: false, status: res.status };
    if (res.status === 204) return { ok: true, status: 204 };
    return { ok: true, status: res.status, data: await res.json().catch(() => undefined) };
  },
  async post(endpoint: string, body: unknown, opts?: RequestOptions) {
    return fetchWithRetry(`/api${endpoint}`, {
      method: 'POST', headers: BASE_HEADERS, body: JSON.stringify(body),
    }, opts);
  },
  async put(endpoint: string, body: unknown, opts?: RequestOptions) {
    return fetchWithRetry(`/api${endpoint}`, {
      method: 'PUT', headers: BASE_HEADERS, body: JSON.stringify(body),
    }, opts);
  },
  async patch(endpoint: string, body: unknown, opts?: RequestOptions) {
    return fetchWithRetry(`/api${endpoint}`, {
      method: 'PATCH', headers: BASE_HEADERS, body: JSON.stringify(body),
    }, opts);
  },
  async delete(endpoint: string, opts?: RequestOptions) {
    return fetchWithRetry(`/api${endpoint}`, { method: 'DELETE' }, opts);
  },
  async upload(endpoint: string, formData: FormData, opts?: RequestOptions) {
    return fetchWithRetry(`/api${endpoint}`, {
      method: 'POST', body: formData,
    }, opts);
  },
};
