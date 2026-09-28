/**
 * Pure predicate for the right-panel "ready" gate in ProjectPage.
 *
 * Extracted from ProjectPage so the gate decision matrix is unit-testable.
 *
 * INVARIANT: gate panel content (and tab highlight) on prefs hydration AND on the
 * URL-driven document being committed — BUT only force the loading state when there
 * is nothing valid to fall back on.  Why: gate on prefs+committed-doc, but only force loading when there's no valid fallback — avoids a flash when a stale-but-valid tab can show.
 *
 * - Cold load / deep-link / F5: prefs flip to loaded BEFORE the doc commits, so
 *   `currentDocId` is null. The per-doc slice would fall back to DEFAULT_DOC_STATE
 *   and the panel would flick default → real saved tab (and mount a panel only to
 *   discard it). Here we keep the loading state on: NOT ready.
 * - In-app navigation: `currentDocId` is the PREVIOUS doc (non-null). We keep the
 *   current panel mounted until the new doc commits, so the inner chatScopeLoading
 *   spinner becomes the single loading indicator and ChatPanel does NOT unmount.
 *   Why: a bare PanelLoading frame + a remount that briefly flashes the previous
 *   document's chat ("spinner → stale chat → spinner → new chat") is the reported
 *   double-blink; keeping the panel mounted collapses it to a single transition.
 */
export function isRightPanelReady(params: {
  projectPrefsLoaded: boolean;
  docPending: boolean;
  currentDocId: string | null;
}): boolean {
  const { projectPrefsLoaded, docPending, currentDocId } = params;
  // In-app nav (currentDocId != null, pending) → ready: keep the previous panel
  // mounted until the new doc commits. Cold load (currentDocId == null, pending)
  // → not ready: nothing valid to show, hold PanelLoading.
  return projectPrefsLoaded && (!docPending || currentDocId != null);
}
