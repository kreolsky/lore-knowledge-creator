/** The project's memory-ON persona ids for the persona picker's Brain mark — local state, one consumer (ChatInput). */

import { useEffect, useState } from 'react';
import { apiClient } from '../../api/client';
import { useAppStore } from '../../store/app-store';
import { t } from '../../i18n';
import type { Document } from '../../types';

/**
 * Ids of `personas` whose frontmatter turns conversation memory ON (GET
 * /chat/memory-personas). WHY keyed on each persona's updated_at: the flag lives
 * in the persona's frontmatter, so an edit of a persona must re-read it.
 * A failed read toasts and leaves the set empty — the marks are absent, not stale.
 */
export function useMemoryPersonas(projectId: string | undefined, personas: Document[]): Set<string> {
  const [ids, setIds] = useState<Set<string>>(new Set());
  const showToast = useAppStore(s => s.showToast);
  const key = personas.map(p => `${p.document_id}@${p.updated_at}`).join(',');

  useEffect(() => {
    if (!projectId || !key) { setIds(new Set()); return; }
    const ctrl = new AbortController();
    apiClient.get(`/chat/memory-personas?project_id=${encodeURIComponent(projectId)}`, { signal: ctrl.signal })
      .then((res: { persona_ids: string[] }) => {
        if (!ctrl.signal.aborted) setIds(new Set(res.persona_ids));
      })
      .catch(() => {
        if (ctrl.signal.aborted) return;
        setIds(new Set());
        showToast(t('chatMemoryPersonasFailed'), 'error');
      });
    return () => ctrl.abort();
  }, [projectId, key, showToast]);

  return ids;
}
