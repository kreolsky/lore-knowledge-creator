/**
 * Editor bridge lifecycle orchestrator.
 *
 * ARCH: the editor exposes a few module-level "bridge" singletons (the transclusion map,
 * per-user caches) because CM6 plugins and non-React callers structurally cannot read
 * React Context. resetEditorHost({scope}) is the SINGLE orchestrator that clears bridge
 * state on a project switch or a user logout. The two scopes deliberately run DIFFERENT
 * clear-sets (see the per-scope rules below). The active-editor slots are owned and
 * mutated by editor/active-editor.
 *
 * ARCH: a leaf registry, the shape of store/chat-store/reset-registry.ts. app-store and
 * the logout paths sit in the entry and call this synchronously; every cache owner
 * registers its own clear at module load, so the entry never reaches CodeMirror, KaTeX
 * or Yjs through this reset.
 */
// SYSTEM: editor-host — bridge lifecycle reset (project switch / logout)

export type ResetScope = 'project' | 'user';

// INVARIANT(security): this module imports nothing, and a clear registers from the module that
// owns the cache, at its top level, never from an orchestrator or a mount effect.
// Why: a cache can hold data only after its owner evaluated, and evaluating is what registers
// it, so a reset that finds no handler for a cache has nothing to clear: a project switch on
// /projects with the editor never loaded is a correct no-op, in any load order. An orchestrator
// registering for its owners would leave a window where a loaded cache is not cleared, because
// DocumentTree, chat and ParentPickerPopup load the preview caches without the editor, and a
// document body from one project or user would then survive the reset.
const handlers: Record<ResetScope, Set<() => void>> = { project: new Set(), user: new Set() };

/** Register a cache clear for a scope, at the owner's module load. Idempotent (dedupes by reference). */
export function registerEditorHostReset(scope: ResetScope, clear: () => void): void {
  handlers[scope].add(clear);
}

/**
 * Clear editor bridge state for a project switch or a user logout.
 *
 * scope:'project' (project switch) clears the transclusion-related bridges ONLY:
 *   transcludeMap + widgetHeightCache + docPreviewCache + refPreviewCache.
 *   INVARIANT (app-store): cross-project transclusion targets never leak / no silent
 *   stale content. The 'project' registrations are the single source for that clear-set.
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
 * Synchronous, and never throws: a throw is isolated so one failing clear cannot skip
 * the rest (same contract as store/chat-store/reset-registry.ts).
 */
export function resetEditorHost({ scope }: { scope: ResetScope }): void {
  for (const clear of handlers[scope]) {
    try {
      clear();
    } catch (e) {
      console.error('editor-host reset handler failed', e);
    }
  }
}
