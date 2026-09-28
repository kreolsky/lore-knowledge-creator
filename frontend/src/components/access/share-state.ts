/**
 * Pure share-state derivation + toggle planner for the two-checkbox General access
 * section (see SYSTEM: access-panel).
 *
 * WHY pure: the two-checkbox semantics (one share per document, checkbox state derived
 * from the server list, toggle → a plan of API calls) are the load-bearing logic, so
 * they live here — fully testable without a React tree or a live view. The
 * GeneralAccessSection component renders `deriveShareState` and executes
 * `planShareToggle`; the AccessPanel.test.tsx wiring stays a smoke tier.
 *
 * Semantics: one document has at most one share; the UI is two checkboxes. State is
 * DERIVED from the server list (not a local flag), so it stays honest after a failed
 * write. A scope change is an atomic PATCH, never revoke-then-create. When no own row
 * exists but an ancestor's subtree share publishes this doc, the summary is
 * "inherited" (never "not-shared") and the checkboxes stay off but ENABLED — ticking
 * one mints this document's own root.
 */

export interface ShareRow {
  share_id: string;
  scope: 'doc' | 'subtree';
}

export interface InheritedFrom {
  document_id: string;
  title: string;
}

export type ShareSummary = 'not-shared' | 'doc' | 'subtree' | 'inherited';

export interface ShareState {
  /** An own share row exists for this document. */
  shared: boolean;
  /** An own subtree-scope row exists (implies shared). */
  subtree: boolean;
  /** Which summary sentence to render. */
  summary: ShareSummary;
  /** The Include-subtree checkbox is greyed only when there is nothing to widen onto —
   *  not shared AND not inherited (inherited coverage keeps it enabled). */
  subtreeDisabled: boolean;
}

export type ToggleWhich = 'shared' | 'subtree';

export type ShareAction =
  | { kind: 'create'; scope: 'doc' | 'subtree' }
  | { kind: 'patch'; shareId: string; scope: 'doc' | 'subtree' }
  | { kind: 'revoke'; shareId: string };

export interface TogglePlan {
  actions: ShareAction[];
}

/**
 * Derive the checkbox state from the server's share list (+ the inherited coverage
 * the LIST endpoint reports). Legacy multi-row inputs collapse to one coherent
 * state: shared if any row exists, subtree if any subtree row exists.
 */
export function deriveShareState(
  shares: ShareRow[],
  inheritedFrom: InheritedFrom | null,
): ShareState {
  const shared = shares.length > 0;
  const subtree = shares.some(s => s.scope === 'subtree');
  const summary: ShareSummary = shared
    ? (subtree ? 'subtree' : 'doc')
    : (inheritedFrom ? 'inherited' : 'not-shared');
  // The subtree checkbox is enabled whenever there is an own row to widen OR an
  // inherited coverage to "upgrade" with an own root. Only the bare not-shared state
  // greys it (disabled-until-shared wiring).
  const subtreeDisabled = !shared && !inheritedFrom;
  return { shared, subtree, summary, subtreeDisabled };
}

/**
 * Plan the API calls for a checkbox toggle. Returns a list of actions the component
 * executes optimistically (with rollback + toast on failure). An empty list is a
 * no-op (e.g. turning subtree off when no subtree row exists).
 */
export function planShareToggle(
  state: ShareState,
  which: ToggleWhich,
  shares: ShareRow[],
): TogglePlan {
  if (which === 'shared') {
    if (state.shared) {
      // Turning Share OFF → revoke EVERY row (one click fully unshares; also cleans
      // up any legacy multi-row share).
      return { actions: shares.map(s => ({ kind: 'revoke', shareId: s.share_id })) };
    }
    // Turning Share ON → mint a doc-scope share.
    return { actions: [{ kind: 'create', scope: 'doc' }] };
  }

  // which === 'subtree'
  if (state.subtree) {
    // Turning subtree OFF → narrow EVERY subtree row to doc (share stays on). Legacy
    // documents can carry several subtree rows (the pre-rework UI minted freely);
    // narrowing only the first would leave the checkbox stuck checked. Mirrors the
    // shared-OFF revoke-all reconciliation.
    return {
      actions: shares
        .filter(s => s.scope === 'subtree')
        .map(s => ({ kind: 'patch', shareId: s.share_id, scope: 'doc' })),
    };
  }
  // Turning subtree ON. With an own row, PATCH it to subtree (atomic widen). With
  // no own row (inherited or fresh-but-enabled), CREATE a subtree share — this is
  // the "checking Include subtree implies sharing" path: one action turns both on.
  if (shares.length > 0) {
    return { actions: [{ kind: 'patch', shareId: shares[0].share_id, scope: 'subtree' }] };
  }
  return { actions: [{ kind: 'create', scope: 'subtree' }] };
}
