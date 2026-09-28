/**
 * Generic entity-collab control frames: presence, access, status, errors.
 *
 * The entity-GENERIC half of the JSON control-frame dispatch (frames every
 * entity kind would have); the doc-specific half lives in doc-callbacks.ts.
 * Frames needing provider internals (awareness teardown, flush waiters,
 * telemetry) stay in yjs-provider's own switch.
 */

import type { AccessLevel } from '../types';

/**
 * Presence payload as sent by the server (`init.users` / `user_joined`).
 * `access_level` is included by the current backend so the client can filter presence
 * to editors; it is optional here for forward/backward robustness (older payloads omit
 * it, in which case the consumer treats the user as non-editor for the live signal).
 */
export interface PresencePayload {
  user_id: string;
  name: string;
  access_level?: AccessLevel;
}

/** Entity-generic collab callbacks; doc-channel callbacks extend these (doc-callbacks.ts). */
export interface YjsCollabCallbacks {
  onPresenceUsers: (users: PresencePayload[]) => void;
  onUserJoined: (user: PresencePayload) => void;
  onUserLeft: (userId: string) => void;
  onAccessChanged: (level: string) => void;
  onAccessRevoked: () => void;
  onStatusChange: (status: import('./ws-status').WsStatus) => void;
  onSynced: () => void;
  onError: (message: string) => void;
}

export function isString(v: unknown): v is string { return typeof v === 'string'; }
export function isObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}
export function isUserPayload(v: unknown): v is PresencePayload {
  return isObject(v) && isString(v.user_id) && isString(v.name);
}

/**
 * Dispatch an entity-generic control frame to every bundle.
 * Returns true when the frame type was consumed (even if its payload guard
 * dropped it) — an unhandled type returns false so the caller can try the
 * doc-layer table next.
 */
export function dispatchEntityFrame(
  msg: Record<string, unknown>,
  bundles: YjsCollabCallbacks[] | null,
): boolean {
  switch (msg.type) {
    case 'init': {
      if (bundles) {
        broadcastToBundles(bundles, cb => cb.onStatusChange('connected'));
        if (Array.isArray(msg.users)) {
          const users = msg.users.filter(isUserPayload);
          broadcastToBundles(bundles, cb => cb.onPresenceUsers(users));
        }
      }
      return true;
    }

    case 'user_joined':
      if (bundles && isUserPayload(msg.user)) {
        const user = msg.user;
        broadcastToBundles(bundles, cb => cb.onUserJoined(user));
      }
      return true;

    case 'access_changed':
      if (bundles && isString(msg.level)) {
        const level = msg.level;
        broadcastToBundles(bundles, cb => cb.onAccessChanged(level));
      }
      return true;

    case 'access_revoked':
      if (bundles) broadcastToBundles(bundles, cb => cb.onAccessRevoked());
      return true;

    case 'error':
      if (isString(msg.message)) {
        const message = msg.message;
        if (bundles) broadcastToBundles(bundles, cb => cb.onError(message));
        else console.warn('[yjs-provider] error:', message);
      }
      return true;

    default:
      return false;
  }
}

/**
 * Dispatch a callback to EVERY registered consumer bundle of an entity.
 * Iterates a SNAPSHOT of the list so a bundle that synchronously leaves (splice)
 * during dispatch cannot skip or double-process a neighbour.
 */
export function broadcastToBundles<T>(bundles: T[], fn: (cb: T) => void): void {
  for (const cb of [...bundles]) {
    fn(cb);
  }
}
