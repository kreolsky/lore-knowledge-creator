/**
 * EditorRoutes — the project-shell routes, one chunk with the whole editor graph.
 *
 * ARCH (plan "route-unification-single-shell"): /projects/:projectId,
 * /docs/:documentId and the legacy nested URL render from ONE statically-linked
 * module (ProjectPage + DocumentPage together). Why: React holds a
 * freshly-mounted Suspense fallback ~300ms before revealing content (measured in
 * a browser drive: fallback lifetime 285–291ms with the module
 * already warm), so ANY component whose lazy resolves for the first time ON a
 * /projects/:id ↔ /docs/:id crossing paints that fallback — the module being
 * loaded changes nothing; only never suspending does. Static linking here makes
 * the crossing suspension-free: both routes' elements are plain imports inside
 * one module, and the chunk price (the CM6 graph rides the editor-shell chunk)
 * is paid exactly where it was already paid — the first /projects/:projectId or
 * /docs/:documentId load, now in ONE chunk instead of two sequential ones.
 *
 * SessionRoute mounts this as the `*` element of its authed branch (behind
 * AuthGuard + Layout); /projects, /cabinet and /admin keep their own light
 * chunks and never download the editor.
 */

import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ProjectPage } from './ProjectPage';
import { DocumentPage } from './DocumentPage';
import { useUIStore } from '../store/ui-store';
import { docUrl } from '../utils/routing';

// Lazy so the editor chunk does not gain the public render path: only a signed-in
// visitor who was actually refused a document ever downloads it.
const PublicSharePage = lazy(() => import('./PublicSharePage').then(m => ({ default: m.PublicSharePage })));

/** Legacy /projects/:projectId/docs/:documentId → /docs/:documentId.
 *
 * plan "public-document-ids": the document uuid is the one canonical URL. This
 * client redirect covers dev (Vite, no nginx); in prod the nginx
 * `location ~ ^/projects/[^/]+/docs/([^/]+)$ { return 301 /docs/$1; }` rewrite
 * fires first. */
function LegacyDocRedirect() {
  const { documentId } = useParams<{ documentId: string }>();
  return <Navigate replace to={docUrl(documentId ?? '')} />;
}

/** The editor shell for a SIGNED-IN visitor, on BOTH /projects/:projectId and
 *  /docs/:documentId: the project shell, or — once this id
 *  has been refused (see publicFallbackDocIds in ui-store) — the same
 *  read-only published view an anonymous visitor gets. Why: a published link must
 *  open for everyone; holding a session for some other project cannot make a visitor
 *  see less than a logged-out one. The public surface is anonymous by construction
 *  (no auth dependency on /api/public/*), so the cookie simply plays no part. */
function DocShellRoute() {
  const { documentId } = useParams<{ documentId: string }>();
  const fallbackIds = useUIStore(s => s.publicFallbackDocIds);
  const markPublicFallback = useUIStore(s => s.markPublicFallback);
  if (documentId && fallbackIds.has(documentId)) {
    return (
      <Suspense fallback={null}>
        <PublicSharePage documentId={documentId} onTreeIds={markPublicFallback} />
      </Suspense>
    );
  }
  return <ProjectPage />;
}

export function EditorRoutes() {
  return (
    <Routes>
      {/* INVARIANT: this element and the /docs/:documentId one are the SAME
          component type. Why: React reconciles by element type at a position, so
          a differing type across the /projects/:id → /docs/:id crossing unmounts
          and remounts the whole shell — the repainted frame that the shared-chunk
          fix removed. DocShellRoute with no :documentId simply IS ProjectPage. */}
      <Route path="/projects/:projectId" element={<DocShellRoute />} />
      <Route path="/projects/:projectId/docs/:documentId" element={<LegacyDocRedirect />} />
      {/* The canonical editor URL: ProjectPage's <Outlet/> mounts the index
          child <DocumentPage>, structurally identical to the legacy nested
          route — same shell, same chunk, no boundary to suspend on. */}
      <Route path="/docs/:documentId" element={<DocShellRoute />}>
        <Route index element={<DocumentPage />} />
      </Route>
      {/* The authed catch-all. This is the only place that can see "no editor
          route matched": SessionRoute's `*` element IS this component, so an
          unknown authed path reaches here and would otherwise render an empty
          <main> inside Layout. */}
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
