/** Pull the refusal reason out of a server error body.
 *
 * Role transitions and register submits have several LEGAL failures
 * (self-change 400, demote-with-group 409, illegal group pointer 422, short
 * password 422) and a generic line hides which one fired. Handles both body
 * shapes: `{"detail": "…"}` and pydantic's `{"detail": [{"msg": "…"}, …]}`. */
import { HttpError } from '../api/client';

/** From an ALREADY-PARSED body (raw-fetch callers: RegisterPage). */
export function refusalDetailFromBody(parsed: unknown): string | null {
  if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      const msg = (detail[0] as { msg?: unknown })?.msg;
      if (typeof msg === 'string' && msg) return msg;
    }
  }
  return null;
}

/** From an apiClient error (its body is a JSON string). */
export function serverRefusalDetail(err: unknown): string | null {
  if (!(err instanceof HttpError)) return null;
  try {
    return refusalDetailFromBody(JSON.parse(err.detail));
  } catch {
    return null;
  }
}
