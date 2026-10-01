/** The running release tag, read from /api/health.
 *
 * Read from the backend rather than a build-time constant: the prod frontend is a
 * static nginx bundle, so a baked-in version would go stale whenever the backend is
 * redeployed without a frontend rebuild. `null` = still loading, `''` = fetch failed
 * (callers render it as an explicit error, never as a blank — no silent degradation).
 */

import { useEffect, useState } from 'react';

export function useAppVersion(): string | null {
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch('/api/health', { credentials: 'include' })
      .then(res => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
      .then(data => { if (!cancelled) setVersion(String(data.version ?? '')); })
      .catch(() => { if (!cancelled) setVersion(''); });
    return () => { cancelled = true; };
  }, []);
  return version;
}
