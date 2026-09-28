/**
 * useAccessTabState — derives the collapsed-tab tint for the Access tab.
 *
 * SYSTEM: access-panel — the tint reflects the STRONGEST active access on the
 * current document, top→down:
 *   red    — an agent key with auto_apply (read-write agent) lives on this doc
 *   yellow — an active anonymous share OR the project is public
 *   blue   — a widget-only key lives on this doc
 *   none   — no external access
 *
 * ARCH (plan "iridescent-wibbling-heron", item 8): human members do NOT drive
 * the color (decided) — only the external/anonymous surface does. The tint is
 * cosmetic; access is still enforced server-side. Re-fetches on doc/project
 * change; tolerates load failure by falling back to 'none' (no silent
 * degradation in the panel itself — the panel surfaces its own errors).
 */
import { useEffect, useState } from 'react';
import { apiClient } from '../api/client';
import { listShares } from '../api/public-share';

export type AccessTabTint = 'none' | 'blue' | 'yellow' | 'red';

interface ApiKeyRow {
  capabilities?: string[];
  auto_apply?: boolean | null;
}

export function useAccessTabState(
  docId: string | null | undefined,
  projectId: string | null | undefined,
  isPublic: boolean,
): AccessTabTint {
  const [tint, setTint] = useState<AccessTabTint>('none');

  useEffect(() => {
    if (!docId || !projectId) { setTint('none'); return; }
    let cancelled = false;
    (async () => {
      try {
        const [keysResp, shares] = await Promise.all([
          apiClient.get(`/api-keys?document_id=${docId}`).catch(() => ({ keys: [] })),
          listShares(projectId, docId).catch(() => ({ shares: [], inherited_from: null })),
        ]);
        if (cancelled) return;
        const keys: ApiKeyRow[] = (keysResp?.keys ?? []) as ApiKeyRow[];
        const hasAgentFull = keys.some(
          (k) => Array.isArray(k.capabilities) && k.capabilities.includes('agent') && k.auto_apply === true,
        );
        const hasWidget = keys.some(
          (k) => Array.isArray(k.capabilities) && k.capabilities.includes('widget'),
        );
        const hasShare = shares.shares.length > 0;
        // Precedence: red > yellow > blue > none.
        const next: AccessTabTint = hasAgentFull
          ? 'red'
          : (hasShare || isPublic)
            ? 'yellow'
            : hasWidget
              ? 'blue'
              : 'none';
        setTint(next);
      } catch {
        if (!cancelled) setTint('none');
      }
    })();
    return () => { cancelled = true; };
  }, [docId, projectId, isPublic]);

  return tint;
}
