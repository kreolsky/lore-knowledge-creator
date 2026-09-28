/**
 * Create a markdown reference, register it in the store, and navigate into it.
 *
 * SYSTEM: reference-create — the single canonical entry point for "Add markdown"
 * (ReferencesPanel.handleAdd) and "Create reference from selection" (selection toolbar's
 * createReference). Both call sites share the auto-title convention
 * (`markdown-YYYY-MM-DD HH:MM`) and the add/focus/navigate sequence, so reference
 * creation stays consistent regardless of how it is triggered.
 *
 * Why this lives here and not in markdown-actions.ts: it is app-level orchestration
 * (API + store + event), not a CodeMirror formatting action. A neutral module lets the
 * panel import it without coupling to the editor actions layer.
 */
import type { Reference } from '../../types';
import { apiClient } from '../../api/client';
import { useAppStore } from '../../store/app-store';
import { emit } from '../../events';

/** Timestamped title in the `markdown-YYYY-MM-DD HH:MM` convention. */
function markdownRefTitle(): string {
  const now = new Date();
  const ts = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  return `markdown-${ts}`;
}

/**
 * POST a new markdown reference (optionally seeded with `content`), add it to the store,
 * request editor focus for when its collab session mounts, and emit navigation to it.
 *
 * Throws on API failure so each caller controls its own error UX (the toolbar shows a
 * toast; the panel lets it propagate). Returns the created reference.
 */
export async function createMarkdownReference(params: {
  projectId: string;
  documentId: string;
  content?: string;
}): Promise<Reference> {
  const ref = await apiClient.post('/references', {
    project_id: params.projectId,
    document_id: params.documentId,
    title: markdownRefTitle(),
    media_type: 'markdown',
    content: params.content ?? '',
  });
  useAppStore.getState().addReference(ref);
  // Focus the editor once the new reference's collab session is ready, so the
  // user can start typing immediately instead of clicking back into the editor.
  useAppStore.getState().setPendingEditorFocus(ref.reference_id);
  emit('navigate-to-reference', { referenceId: ref.reference_id, stayInContext: true });
  return ref;
}
