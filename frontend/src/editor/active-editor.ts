/**
 * The one answer to "which editor is active": the focused CM6 EditorView, the host (root)
 * view, the focused column's role and content type, the focused collab handle, the
 * role-keyed column views and the view→entity map — every slot, its listeners and its
 * mutators.
 *
 * ARCH: module-level, not React Context — CM6 extensions (table-block-widget), hooks run in
 * ProjectPage's own body (useVoiceInput, useRecordingHotkey) and dev window hooks cannot read
 * Context, and React components read the same slots through useEditorView()/useEditorContent().
 * One store means a non-React write (a focused table cell) is what React readers see, with no
 * mirror to keep in sync.
 *
 * ARCH: TWO view slots, one source per concern:
 *   - `focusedView` — whatever is focused RIGHT NOW (the host editor OR a focused
 *     table cell's nested EditorView). Feeds the selection toolbar, voice input, and
 *     anything that must follow the caret into a cell. getEditorView / useEditorView.
 *   - `rootView` — the HOST editor view ONLY, never a cell. The content-capture source:
 *     useEditorContent() reads the root so a manual snapshot (Cmd+S)
 *     stores the document text (anchor + body), not the focused cell's text.
 *   claimFocus (a column gains focus) and mountView set BOTH; claimNestedView (a cell
 *   focuses) sets ONLY focusedView, leaving rootView on the host. See the INVARIANT on
 *   rootView below.
 *
 * Mutators: claimFocus (column focus: role + content type + both views + handle),
 * claimNestedView (cell focus), mountView/unmountView (Editor mount / entity-switch
 * teardown: both views, role untouched), publishHandle/releaseHandle (useEditorCollab's
 * guarded focused-slot claim). Every clear carries the previous-match guard.
 *
 * Lives in `editor/` with type-only imports of collab so CM6 widgets can import it without
 * the cycle table-block-widget → editor-host → widgets → render-bundle → table-block-widget.
 */
// SYSTEM: active-editor — focused/root view, focused role + handle, role views, view→entity

import type { EditorView } from '@codemirror/view';
import type { EntityYjsState } from '../collab/yjs-provider';
import { serializeTables } from '../components/editor/live-preview/table-block-model';

export type EditorRole = 'primary' | 'secondary';

// ─── Focused column: role + content type ────────────────────────────────────
// INVARIANT: defaults to 'primary' so single-editor mode (one instance, role='primary')
// behaves exactly as before — getFocusedRole() is never 'secondary' unless a reference
// column actually gained focus. Why: snapshot/Cmd+S gating relies on this default.
let focusedRole: EditorRole = 'primary';

// Parallel content-type slot: whether the focused editor is rendering a reference
// (vs the document). Role describes column geometry (which slot is mounted) and cannot
// distinguish split-primary (document) from single-editor-primary-rendering-a-reference.
// Note parenting must follow the focused editor's CONTENT TYPE, matching the anchor
// (getActiveHandle), which already tracks the same focused editor.
// INVARIANT: defaults to false — the document is the default/primary entity, so the
// default matches getFocusedRole() defaulting to 'primary'. Why: mirrors
// getFocusedRole()'s default + sole-mutator contract so focus + role stay in sync.
// Updated ONLY by claimFocus (same sole-mutator contract as role); NOT reset on
// unmount/entity-switch — relies on the next editor gaining focus, identical to how
// role behaves (no extra teardown logic).
let focusedIsReference = false;

export function getFocusedRole(): EditorRole {
  return focusedRole;
}

export function getFocusedIsReference(): boolean {
  return focusedIsReference;
}

// ─── Focused / root view ─────────────────────────────────────────────────────

// Toolbar/selection source: the host editor OR a focused table cell.
let focusedView: EditorView | null = null;
// INVARIANT(corruption): the root slot holds the HOST editor view ONLY, never a
// focused table cell's nested EditorView. Why: a lost-snapshot bug — getContent()
// captures checkpoint content from the root view; before this split a focused cell's
// view was mirrored into the same slot the toolbar used, so getContent() read the
// cell doc and a manual snapshot (Cmd+S) stored the cell's text instead of the
// document (`![label](table:id)` anchor + full body). claimNestedView sets ONLY
// focusedView; claimFocus + Editor mount set both. Only content capture (getContent)
// reads the root — the toolbar/selection/voice consumers keep reading focusedView so
// they follow the caret into a cell.
let rootView: EditorView | null = null;

function setFocusedView(view: EditorView | null, previous?: EditorView): void {
  // INVARIANT: only clear if previous matches current — prevents a snapshot
  // unmount from nulling the main editor's view. Why: an unmount clears with its
  // own view as `previous`; without the previous-match guard a stale `previous`
  // would null the main editor's view, so the clear only happens when `previous`
  // is still the current view.
  if (view === null && previous !== undefined && previous !== focusedView) return;
  focusedView = view;
}

function setRootView(view: EditorView | null, previous?: EditorView): void {
  // Same previous-match guard as setFocusedView: a secondary column's unmount must not
  // null the primary editor's root view.
  if (view === null && previous !== undefined && previous !== rootView) return;
  rootView = view;
}

/** Return the focused EditorView (host or cell), or null if unmounted. */
export function getEditorView(): EditorView | null {
  return focusedView;
}

/** Return the HOST (root) EditorView — the content-capture source — or null if unmounted. */
export function getRootEditorView(): EditorView | null {
  return rootView;
}

/** The root (host) view's text, or `fallback` when no editor is mounted. */
function getContent(fallback = ''): string {
  // INVARIANT: content capture reads the ROOT (host) view, not the focused one. Why:
  // a manual snapshot must store the document text, not a focused table cell's text.
  return rootView?.state.doc.toString() ?? fallback;
}

/** Focused-view getter for React components (toolbar, selection, scroll). Stable identity. */
export function useEditorView(): () => EditorView | null {
  return getEditorView;
}

/** Content-capture getter for React components (Cmd+S, create-snapshot). Stable identity. */
export function useEditorContent(): (fallback?: string) => string {
  return getContent;
}

// ─── Focused collab handle ───────────────────────────────────────────────────
// The FOCUSED handle — what markdown actions, hotkeys and Cmd+S want: whatever editor holds
// focus RIGHT NOW. Identity-scoped consumers (table widget, badge list, panel table ops,
// paste) resolve by entity instead: getEntityHandle in collab/active-handle-registry.
// Readers MUST tolerate `null` — there's a small window between editor mount and collab
// init where no handle is registered.
let focusedHandle: EntityYjsState | null = null;

export function getActiveHandle(): EntityYjsState | null {
  return focusedHandle;
}

/** Claim the focused-handle slot (useEditorCollab's guarded publish). */
export function publishHandle(handle: EntityYjsState): void {
  focusedHandle = handle;
}

/** Clear the focused-handle slot only while it still holds `previous` — a column's
 *  teardown must not wipe the handle another column claimed. `undefined` clears
 *  unconditionally (same contract as unmountView). */
export function releaseHandle(previous?: EntityYjsState): void {
  if (previous !== undefined && focusedHandle !== previous) return;
  focusedHandle = null;
}

/**
 * Serialize the focused editor's `tables` subtree to JSON for checkpoint capture.
 *
 * Returns `'{}'` when no handle is registered (mount race) or the doc has no tables.
 * Why a string (not null): the manual snapshot path always sends a tables payload so the
 * unified writer stores `tables_json` + `tables_hash`; `'{}'` is the legitimate "no
 * tables" value and is still hashed (distinguishable from a legacy None on the backend).
 */
export function getActiveTablesJson(): string {
  const handle = getActiveHandle();
  if (!handle?.ydoc) return '{}';
  return serializeTables(handle.ydoc);
}

// ─── Mutators ────────────────────────────────────────────────────────────────

/** Inputs for a focus claim from an Editor column. */
export interface FocusClaim {
  role: EditorRole;
  /** Whether the focused editor is rendering a reference (vs the document). Required so
   *  every caller is explicit and a future caller cannot silently mis-parent a note by
   *  omitting it. Note parenting follows content type, matching the anchor (handle). */
  isReference: boolean;
  view: EditorView;
  handle: EntityYjsState | null;
}

/** Claim all focus-sensitive slots for the focused column. The focused column's
 *  host is BOTH the toolbar target (focusedView) and the content-capture root (rootView). */
export function claimFocus({ role, isReference, view, handle }: FocusClaim): void {
  focusedRole = role;
  focusedIsReference = isReference;
  setFocusedView(view);
  setRootView(view);
  focusedHandle = handle;
}

/** Point the focused view at a nested (cell) view, or back to its host.
 *  INVARIANT: sets ONLY focusedView, never rootView. Why: a lost-snapshot bug —
 *  getContent() captures checkpoint content from the root (host) view; if a cell
 *  view overwrote the root, a manual snapshot (Cmd+S) stored the cell's text
 *  instead of the document. The cell drives the toolbar (focusedView); the host
 *  stays the content source. Deliberately does NOT touch handle/role — a cell
 *  lives inside the focused column. */
export function claimNestedView(view: EditorView): void {
  setFocusedView(view);
}

/** An Editor column's view was created: point focused + root at it. Does NOT change the
 *  focused role — only a focus claim does. */
export function mountView(view: EditorView): void {
  setFocusedView(view);
  setRootView(view);
}

/** An Editor column's view is going away: clear focused + root only while they still hold
 *  `previous` (previous-match guard). `undefined` clears unconditionally. */
export function unmountView(previous?: EditorView): void {
  setFocusedView(null, previous);
  setRootView(null, previous);
}

// ─── Role-keyed view registry (split view) ──────────────────────────────────
// ARCH: A THIRD registry concern, orthogonal to focused/root. In split mode two
// Editor instances (role='primary' = document, role='secondary' = reference) mount
// simultaneously. Non-React UI — the note connector line and the click-to-focus
// scroll — must reach a column's view BY ROLE, not by whichever column happens to
// be focused. The role map holds one view per role so a connector can resolve the
// owning anchor even after the user moves keyboard focus to the OTHER column.
// INVARIANT: each role slot clears independently with the same `previous`-match
// guard as the focused/root slots. Why: a secondary column's unmount
// (split exit, reference close) must NOT null the primary slot and vice-versa.
const roleViews = new Map<EditorRole, EditorView>();
type RoleViewListener = (role: EditorRole, view: EditorView | null) => void;
const roleViewListeners = new Set<RoleViewListener>();

/** Subscribe to role-view changes (mount/unmount of either column's view). Lets the
 *  note connector recompute coords once the secondary column's view is ready. */
export function subscribeRoleView(fn: RoleViewListener): () => void {
  roleViewListeners.add(fn);
  return () => { roleViewListeners.delete(fn); };
}

/** Register a column's EditorView under its role. `view === null` (unmount) clears
 *  the slot only if `previous` matches the stored view for that role — mirroring the
 *  previous-match guard on the focused/root slots so a remount of one column
 *  cannot clobber the other. */
export function setRoleView(role: EditorRole, view: EditorView | null, previous?: EditorView): void {
  if (view === null) {
    if (previous !== undefined && previous !== roleViews.get(role)) return;
    if (!roleViews.has(role)) return;
    roleViews.delete(role);
    roleViewListeners.forEach((l) => l(role, null));
    return;
  }
  if (roleViews.get(role) === view) return;
  roleViews.set(role, view);
  roleViewListeners.forEach((l) => l(role, view));
}

/** Return the view registered for a role, or null if that column is unmounted. */
export function getRoleView(role: EditorRole): EditorView | null {
  return roleViews.get(role) ?? null;
}

// ─── View → entity registry ──────────────────────────────────────────────────
// ARCH: a FOURTH registry concern. Identity-scoped non-React consumers (the table
// widget's late-bind, paste/drop model placement) must resolve WHICH entity a
// given EditorView renders — the focused slot cannot answer that in split view
// (it points at whichever column holds focus). WeakMap: a destroyed view's entry
// is garbage-collected, no explicit teardown needed.
const viewEntities = new WeakMap<EditorView, string>();

/** Register the entity a view renders. `null` (or view destruction) clears it. */
export function setViewEntity(view: EditorView, entityId: string | null): void {
  if (entityId === null) viewEntities.delete(view);
  else viewEntities.set(view, entityId);
}

/** Return the entity id a view renders, or null for unregistered views. */
export function getViewEntity(view: EditorView): string | null {
  return viewEntities.get(view) ?? null;
}
