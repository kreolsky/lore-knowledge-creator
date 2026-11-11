/**
 * 'outdated' = this tab speaks a collab protocol the server no longer accepts (close
 * 4010, see ws-close-codes.ts) — permanent, no reconnect; the only fix is a reload.
 */
export type WsStatus = 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'outdated';
