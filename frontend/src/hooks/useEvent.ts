/**
 * Subscribe to a typed event bus event with automatic cleanup on unmount.
 *
 * ARCH: Uses useRef to hold the latest callback so the event bus subscription
 * is stable for the lifetime of the component. This prevents subscribe/unsubscribe
 * churn when callers pass inline or unstable callback references.
 */

import { useEffect, useRef } from 'react';
import { on, off } from '../events';
import type { EventMap } from '../events';

type Callback<T> = (payload: T) => void;

export function useEvent<K extends keyof EventMap>(
  event: K,
  callback: Callback<EventMap[K]>,
): void {
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    const handler = ((payload: EventMap[K]) => callbackRef.current(payload)) as Callback<EventMap[K]>;
    on(event, handler);
    return () => off(event, handler);
  }, [event]);
}
