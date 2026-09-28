/** Shared WebSocket close-code policy for permanent (auth/lifecycle) closures.
 *
 *  // ARCH: both WS clients (yjs-provider collab + project-connection lifecycle) treat
 *  //       these codes as permanent — reconnecting cannot fix them, so they stop the
 *  //       backoff loop instead of hammering the server. Centralized here so the two
 *  //       clients never drift (incident: project-ws reconnected 50× after a 4003 kick).
 *
 *  4001 = Unauthorized, 4003 = No access (revoked), 4004 = Not found.
 *  4008 (heartbeat timeout) is intentionally NOT here — it is transient and SHOULD reconnect.
 */
// SYSTEM: ws-close-codes — permanent WS close codes shared across collab + project clients

export const AUTH_CLOSE_CODES: readonly number[] = [4001, 4003, 4004];

export function isAuthCloseCode(code: number): boolean {
  return AUTH_CLOSE_CODES.includes(code);
}
