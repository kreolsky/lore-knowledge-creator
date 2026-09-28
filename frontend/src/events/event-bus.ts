/** Module-level typed event bus — synchronous fire-and-forget pub/sub.
 *
 * Frontend mirror of backend/event_bus.py. Decouples component communication
 * from direct window.dispatchEvent/addEventListener.
 */
// ARCH: Sync pub/sub mirrors backend async event_bus — decouples components without prop drilling.
// SYSTEM: event-bus — synchronous typed pub/sub for component communication

import type { EventMap } from './event-types';

type Callback<T> = (payload: T) => void;

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- heterogeneous map storing callbacks of different event types
const _subscribers = new Map<string, Set<Callback<any>>>();

export function on<K extends keyof EventMap>(
  event: K,
  callback: Callback<EventMap[K]>,
): void {
  if (!_subscribers.has(event)) _subscribers.set(event, new Set());
  _subscribers.get(event)!.add(callback);
}

export function off<K extends keyof EventMap>(
  event: K,
  callback: Callback<EventMap[K]>,
): void {
  _subscribers.get(event)?.delete(callback);
}

export function emit<K extends keyof EventMap>(
  ...args: EventMap[K] extends void ? [event: K] : [event: K, payload: EventMap[K]]
): void {
  const [event, payload] = args as [K, EventMap[K]];
  const subs = _subscribers.get(event);
  if (!subs) return;
  for (const cb of subs) {
    try {
      cb(payload);
    } catch (err) {
      // WHY: log only — one throwing subscriber must not stop delivery to the rest;
      // a subscriber that owns a user-facing op reports its own failure.
      console.error(`Event bus subscriber error for "${event}":`, err);
    }
  }
}
