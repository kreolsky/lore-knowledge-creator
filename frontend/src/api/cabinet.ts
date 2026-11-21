/** Cabinet REST helpers: credentialed fetch with the page's error contract, and the account-PIN check. */

import { apiClient, HttpError } from './client';

/** PATCH/POST/DELETE (body) or GET (body=null) to cabinet endpoints with error
 * handling. Returns [data, errorMsg]. */
export async function cabinetFetch(
  endpoint: string,
  body: Record<string, unknown> | null,
  method = 'PATCH',
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- callers access untyped JSON fields
): Promise<[any, string]> {
  const res = await fetch(`/api/cabinet/${endpoint}`, {
    method,
    ...(body === null ? {} : {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
    credentials: 'include',
  });
  if (res.status === 401) {
    window.location.href = '/';
    return [null, ''];
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = data?.detail ?? `Error ${res.status}`;
    return [null, msg];
  }
  return [data, ''];
}

export type PinVerdict = 'ok' | 'wrong' | 'rateLimited' | 'failed';

/** Check the account PIN — the one call behind the app lock and the page lock.
 * WHY: a non-2xx is never a wrong PIN — only a 2xx body carries `valid`; a 429 is the
 * server's attempt limit, told apart so a locked-out user is not shown "connection error". */
export async function verifyPin(pin: string): Promise<PinVerdict> {
  try {
    const data = await apiClient.post('/cabinet/verify-pin', { pin });
    return data.valid ? 'ok' : 'wrong';
  } catch (err) {
    return err instanceof HttpError && err.status === 429 ? 'rateLimited' : 'failed';
  }
}

/** The message key for every verdict but `ok` — shared by both lock screens. */
export const PIN_VERDICT_MESSAGE = {
  wrong: 'wrongPin',
  rateLimited: 'tooManyAttempts',
  failed: 'connectionError',
} as const satisfies Record<Exclude<PinVerdict, 'ok'>, string>;
