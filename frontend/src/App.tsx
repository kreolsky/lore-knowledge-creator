/** Root component: BrowserRouter, global Toast, top-level route split. */

import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Toast } from './components/Toast';
// ARCH: every page is route-split
// via React.lazy so the main bundle shed SessionRoute (probe + AuthGuard +
// Layout + Header/NavigationTabBar/PinLockScreen) and every page below it
// (editor + its CM6/markdown/mermaid/katex deps) into per-route chunks.
// Authenticated users land on /projects/... and never download the Landing chunk.
const LandingPage = lazy(() => import('./pages/LandingPage').then(m => ({ default: m.LandingPage })));
// Public invite-link registration (/register/:token) — outside SessionRoute so
// a cold visit pays no session probe (same posture as /).
const RegisterPage = lazy(() => import('./pages/RegisterPage').then(m => ({ default: m.RegisterPage })));
// ARCH (plan "public-document-ids"): /s/:token is a one-shot resolver that
// <Navigate replace>s to /docs/<root_id> (TokenRedirect).
const TokenRedirect = lazy(() => import('./pages/TokenRedirect').then(m => ({ default: m.TokenRedirect })));
// ARCH (plan "route-unification-single-shell"): SessionRoute is the ONE element
// of /* and owns the session decision (probe → authed shell | anonymous public
// surface | retry). Hoisting the decision above the shell makes crossing
// /projects/:id ↔ /docs/:id an inner route change — the shell, the guard and
// the probe stay mounted. `/` and `/s/:token` stay OUTSIDE the decider so a
// cold landing visit pays no probe (LandingPage runs its own /auth/me bounce).
const SessionRoute = lazy(() => import('./pages/SessionRoute').then(m => ({ default: m.SessionRoute })));

/** Non-null route Suspense fallback. Why: the editor route (ProjectPage/DocumentPage)
 *  previously had no fallback (null) — React.lazy needs an explicit frame so a chunk
 *  load doesn't blank the viewport. Mirrors the EditorOfflineOverlay spinner. */
function RouteLoading() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="w-6 h-6 border-2 border-text-dim border-t-transparent animate-spin" />
    </div>
  );
}

export default function App() {
  return (
      <BrowserRouter>
        {/* ARCH: single global Toast, mounted OUTSIDE <Routes>. Why: one
            root-level instance covers ALL routes (authed shell, anonymous
            public share, error phase — including surfaces that render without
            Layout); .toast is position:fixed so placement is safe. */}
        <Toast />
        <Routes>
          <Route path="/" element={<Suspense fallback={<RouteLoading />}><LandingPage /></Suspense>} />
          {/* Public registration by invite token (plan moderator-role-foundation). */}
          <Route path="/register/:token" element={<Suspense fallback={<RouteLoading />}><RegisterPage /></Suspense>} />
          {/* Legacy /s/:token → /docs/<root_id> redirect (plan "public-document-ids"). */}
          <Route path="/s/:token" element={<Suspense fallback={<RouteLoading />}><TokenRedirect /></Suspense>} />
          {/* Everything else — /projects…, /docs/:id, /cabinet, /admin — is
              SessionRoute's: it probes once and branches, so no sibling route
              ever remounts the authed shell on a crossing. */}
          <Route path="/*" element={<Suspense fallback={<RouteLoading />}><SessionRoute /></Suspense>} />
        </Routes>
      </BrowserRouter>
  );
}
