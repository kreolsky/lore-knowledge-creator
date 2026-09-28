/** Routing helpers for the unified /docs/<document_id> URL (plan "public-document-ids").
 *
 * The document's uuid is the ONE canonical URL for every audience: members,
 * anonymous published readers, and link generators all use /docs/<id>. There is
 * no per-audience variant and no token in the URL. */

// Matches /projects/<id>… AND /docs/<id>… — the routes that render inside the
// project/editor shell (no Header + NavigationTabBar frame). A bare /projects or
// /docs does NOT match: those are list/top-level pages that keep the frame.
const EDITOR_SHELL_RE = /^\/(?:projects\/[^/]+|docs\/[^/]+)/;

// ONLY the bare /docs/<id> route (no /projects prefix). Used by DocumentPage to
// pick the one-shot /open/{id} cold-path bootstrap: that route carries no
// projectId, so the bundle (not the concurrent per-id calls) must seed
// currentProject. /projects/<id>/docs/<id> does NOT match (it has a projectId).
const BARE_DOC_RE = /^\/docs\/[^/]+/;

/** True for routes that render inside the project/editor shell.
 *
 * Why a predicate: the editor shell was selected by URL SHAPE via the literal
 * regex /^\/projects\/[^/]+/ in 7 places (App.tsx Layout, Header.tsx ×2,
 * UserControls.tsx ×4). /docs/<id> matches none of those, so collapsing them
 * onto ONE predicate is the prerequisite to adding the new route — otherwise the
 * editor would render inside the Header + NavigationTabBar frame. */
export function isEditorShell(pathname: string): boolean {
  return EDITOR_SHELL_RE.test(pathname);
}

/** The canonical document URL: /docs/<document_id>.
 *
 * Single helper for every call site (tree, search, chat Sources, breadcrumbs,
 * share dialog, navigate calls). Supersedes the per-audience
 * `/projects/${pid}/docs/${did}` link shape. */
export function docUrl(documentId: string): string {
  return `/docs/${documentId}`;
}

/** True only for the bare /docs/<id> route (NOT /projects/<id>/docs/<id>).
 *
 * That route has no projectId segment, so the one-shot /open/{id} bundle — not
 * the concurrent per-id fetches — must bootstrap currentProject on cold open. */
export function isBareDocRoute(pathname: string): boolean {
  return BARE_DOC_RE.test(pathname);
}
