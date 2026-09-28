/**
 * Editor bridge lifecycle orchestrator.
 *
 * ARCH: the editor exposes a few module-level "bridge" singletons (the transclusion map,
 * per-user caches) because CM6 plugins and non-React callers structurally cannot read
 * React Context. resetEditorHost({scope}) is the SINGLE orchestrator that clears bridge
 * state on a project switch or a user logout. The two scopes deliberately run DIFFERENT
 * clear-sets (see the per-scope rules below). The active-editor slots are owned and
 * mutated by editor/active-editor.
 */
// SYSTEM: editor-host — bridge lifecycle reset (project switch / logout)

import { clearTranscludeMap } from './live-preview/effects';
import { clearWidgetHeightCache } from './live-preview/widgets';
import { clearPreviewCache } from '../../hooks/useDocumentPreview';
import { clearRefPreviewCache } from '../../hooks/useReferencePreview';
import { clearAllCheckpointDedup } from '../../editor/content-sync';
import { clearPositionCache } from '../../editor/position-cache';
import { clearLastSavedBlobs } from '../../store/ui-store';

export type ResetScope = 'project' | 'user';

/**
 * Clear editor bridge state for a project switch or a user logout.
 *
 * scope:'project' (project switch) clears the transclusion-related bridges ONLY:
 *   transcludeMap + widgetHeightCache + docPreviewCache + refPreviewCache.
 *   INVARIANT (app-store): cross-project transclusion targets never leak / no silent
 *   stale content. This is the single source for that clear-set.
 *   The two preview caches are BOTH transclusion content sources — the transclusion
 *   effect seeds them from one batch and lazy-fetches through both
 *   (useEditorReferenceSync) — so they are cleared together, here, rather than one
 *   here and one as a caller-side sibling.
 *
 * scope:'user' (logout / setCurrentUser(null) / PinLock / UserControls) clears the
 *   per-user in-memory caches ONLY: checkpointDedup + positionCache + lastSavedBlobs.
 *   Why: both caches are module-level Maps keyed by document/user and are never
 *   cleared on logout otherwise → cross-user stale-state leak.
 *
 * The two scopes run DIFFERENT clears by design — do NOT unify them. Unifying would
 * either drop widgetHeight/preview from project-switch (cross-project leak) or add
 * checkpointDedup/positionCache to project-switch (a behaviour change).
 *
 */
export function resetEditorHost({ scope }: { scope: ResetScope }): void {
  if (scope === 'project') {
    clearTranscludeMap();
    clearWidgetHeightCache();
    clearPreviewCache();
    clearRefPreviewCache();
    return;
  }
  // scope === 'user'
  clearAllCheckpointDedup();
  clearPositionCache();
  clearLastSavedBlobs();
}
