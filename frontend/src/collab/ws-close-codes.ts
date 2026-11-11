/** Shared WebSocket close-code policy for permanent (auth/lifecycle) closures.
 *
 *  // ARCH: both WS clients (yjs-provider collab + project-connection lifecycle) treat
 *  //       these codes as permanent — reconnecting cannot fix them, so they stop the
 *  //       backoff loop instead of hammering the server. Centralized here so the two
 *  //       clients never drift (incident: project-ws reconnected 50× after a 4003 kick).
 *
 *  4001 = Unauthorized, 4003 = No access (revoked), 4004 = Not found.
 *  4008 (heartbeat timeout) is intentionally NOT here — it is transient and SHOULD reconnect.
 *
 *  4010 = Client outdated: the server speaks a newer collab protocol than this tab
 *  (the `?proto=` gate in backend/routes/collab_project_ws.py). Permanent like the auth
 *  codes — reconnecting with the SAME build can never fix it — but it is NOT an auth
 *  failure: local mirrors are kept and the surface shows a persistent
 *  "Lore was updated — reload" notice instead of the offline state. Only the NEW client
 *  knows the code: a tab from before the deploy sees its existing disconnected state.
 */
// SYSTEM: ws-close-codes — permanent WS close codes shared across collab + project clients

export const AUTH_CLOSE_CODES: readonly number[] = [4001, 4003, 4004];

export function isAuthCloseCode(code: number): boolean {
  return AUTH_CLOSE_CODES.includes(code);
}

/** The server's "client outdated" close code (see the header). Mirror of the backend
 * `CLIENT_OUTDATED_CLOSE_CODE` in backend/routes/collab_project_ws.py — bump both together. */
export const CLIENT_OUTDATED_CLOSE_CODE = 4010;

export function isClientOutdatedCloseCode(code: number): boolean {
  return code === CLIENT_OUTDATED_CLOSE_CODE;
}
