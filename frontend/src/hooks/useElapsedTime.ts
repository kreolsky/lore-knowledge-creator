/** Timer hook — returns elapsed seconds from a start timestamp, updates ~100ms. */

import { useState, useEffect } from 'react';

/** Returns elapsed seconds since `startedAt` (ms timestamp), updating every second. Returns 0 when null. */
export function useElapsedTime(startedAt: number | null): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!startedAt) { setElapsed(0); return; }
    setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, [startedAt]);
  return elapsed;
}
