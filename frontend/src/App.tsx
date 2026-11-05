/** Root component: BrowserRouter, global Toast, top-level route split. */

import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Toast } from './components/Toast';
import { SessionRoute } from './pages/SessionRoute';
// ARCH: the pages reachable without a session are route-split via React.lazy.
// SessionRoute is the exception, statically linked into the entry, because React
// holds a freshly shown Suspense fallback ~300ms (see EditorRoutes' ARCH) even
// inside a transition: a boundary here would start the session probe, and so the
// whole cold editor load, that late. It is probe-sized; the pages below it stay
// lazy, so the entry does not gain them.
// Authenticated users land on /projects/... and never download the Landing chunk.
const LandingPage = lazy(() => import('./pages/LandingPage').then(m => ({ default: m.LandingPage })));
// Public invite-link registration (/register/:token) — outside SessionRoute so
// a cold visit pays no session probe (same posture as /).
const RegisterPage = lazy(() => import('./pages/RegisterPage').then(m => ({ default: m.RegisterPage })));
// ARCH: /s/:token is a one-shot resolver that <Navigate replace>s to
// /docs/<root_id> (TokenRedirect).
const TokenRedirect = lazy(() => import('./pages/TokenRedirect').then(m => ({ default: m.TokenRedirect })));

/** Non-null route Suspense fallback. Why: React.lazy needs an explicit frame so a
 *  chunk load doesn't blank the viewport. Mirrors the EditorOfflineOverlay spinner. */
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
          {/* Public registration by invite token. */}
          <Route path="/register/:token" element={<Suspense fallback={<RouteLoading />}><RegisterPage /></Suspense>} />
          {/* Legacy /s/:token → /docs/<root_id> redirect. */}
          <Route path="/s/:token" element={<Suspense fallback={<RouteLoading />}><TokenRedirect /></Suspense>} />
          {/* ARCH: SessionRoute is the ONE element of /* and owns the session
              decision (probe → authed shell | anonymous public surface | retry):
              /projects…, /docs/:id, /cabinet, /admin. Hoisting the decision above
              the shell makes crossing /projects/:id ↔ /docs/:id an inner route
              change — the shell, the guard and the probe stay mounted, and no
              sibling route ever remounts them. `/` and `/s/:token` stay OUTSIDE
              the decider so a cold landing visit pays no probe (LandingPage runs
              its own /auth/me bounce). */}
          <Route path="/*" element={<SessionRoute />} />
        </Routes>
      </BrowserRouter>
  );
}
