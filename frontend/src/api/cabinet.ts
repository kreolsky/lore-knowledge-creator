/** Cabinet REST helper: credentialed fetch with the page's error contract. */

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
