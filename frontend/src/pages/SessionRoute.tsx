/**
 * SessionRoute — the /* session decider (plan "route-unification-single-shell").
 *
 * ONE route element owns every authed and public surface except `/` and
 * `/s/:token` (both stay outside so a cold landing visit pays no probe —
 * LandingPage runs its own /auth/me bounce). Crossing /projects/:id ↔ /docs/:id
 * is an INNER route change here: the shell, the guard and this probe all stay
 * mounted, where the old top-level sibling split remounted the whole authed shell
 * per crossing.
 *
 *   - session (probe /auth/me → 200) → AuthGuard (fed the probe's user — this is
 *     the ONE /auth/me a cold authed load pays) → Layout → inner <Routes>:
 *     /projects, /cabinet, /admin as light lazy chunks, and the `*` element
 *     EditorRoutes — ONE chunk with the whole editor graph carrying
 *     /projects/:projectId, /docs/:documentId and the legacy nested URL, so a
 *     crossing between them never renders a lazy for the first time (React
 *     holds a fresh Suspense fallback ~300ms — see EditorRoutes' ARCH).
 *   - anonymous (401) → /docs/:documentId → <PublicSharePage documentId> (the
 *     read-only published view); every other path redirects to `/`.
 *   - error (5xx / network / timeout) → explicit retry screen. This phase can
 *     gate the whole app because it only ever fires cold: the probe runs once
 *     per mount, and a blip during a live session never re-probes. This is the
 *     ONLY handling of a cold 5xx — AuthGuard's lenient ServiceUnavailableError
 *     branch (render children + toast) is gone with its fetch.
 *
 * ARCH: the session probe uses apiClient.probe() (NOT get) so a 401 resolves to
 * {ok:false} instead of triggering apiClient's redirect-on-401 guard — that guard
 * would kick an anonymous visitor off /docs/:id. The probe runs on mount and on
 * each retry; in-tree navigation (any path inside /*) reuses the resolved phase.
 *
 * ARCH (plan "post-public-document-ids-fixes"): the probe distinguishes 401
 * (truly anonymous → public surface) from 5xx / network / timeout (couldn't
 * verify → explicit `error` phase + retry, NOT the public surface). A 500 while
 * a member holds a live cookie must NOT silently render the read-only view —
 * that would violate the no-silent-degradation rule and hide a real outage
 * behind stale UX.
 *
 * ARCH: a malformed :documentId is never rejected client-side. /docs/:documentId
 * cannot collide with /projects, /admin, or /s (distinct path segments), so any
 * id format — uuid, `sys-` deterministic system ids (`_deterministic_id` →
 * `sys-{role}-{project_id}`, including the whole agent-config subtree) — reaches
 * the resolver, which 404s uniformly for unknown ids (no existence oracle).
 *
 * Chunking (ARCH: every page is route-split): this module is probe-sized and
 * lazily mounted by App; every page below (Dashboard, EditorRoutes — which
 * statically owns ProjectPage + DocumentPage and their editor graph — Admin,
 * Cabinet, PublicSharePage) stays React.lazy so the landing bundle never regains
 * the editor and the light authed surfaces (/projects list, /cabinet, /admin)
 * never download it either.
 */

import { lazy, Suspense, useEffect, useState } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { apiClient } from '../api/client';
import type { User } from '../types';
import { AuthGuard } from '../components/AuthGuard';
import { Layout } from '../components/Layout';
import { Button } from '../components/ui';
import { useTranslation } from '../i18n';

const Dashboard = lazy(() => import('./Dashboard').then(m => ({ default: m.Dashboard })));
const AdminPage = lazy(() => import('./AdminPage').then(m => ({ default: m.AdminPage })));
const CabinetPage = lazy(() => import('./CabinetPage').then(m => ({ default: m.CabinetPage })));
// The editor routes (/projects/:id, /docs/:id, legacy nested) are one statically
// linked chunk with the whole editor graph — see EditorRoutes' ARCH. Lazy HERE
// only so /projects, /cabinet and /admin never download it.
const EditorRoutes = lazy(() => import('./EditorRoutes').then(m => ({ default: m.EditorRoutes })));
// Lazy: PublicSharePage pulls PublicEditor + CM6 render path; only the anonymous
// branch needs it.
const PublicSharePage = lazy(() => import('./PublicSharePage').then(m => ({ default: m.PublicSharePage })));

// INVARIANT: 401 is the ONLY probe outcome that resolves to the anonymous surface.
// Every other non-ok outcome (5xx, network throw, timeout) MUST resolve to the
// `error` phase. Why: a 5xx/network blip while a member holds a cookie is a
// transient outage — degrading to the public surface silently shows the wrong
// (read-only) UX and hides the outage. The public surface is reserved for a
// confirmed "no session" (401).
const ANONYMOUS_STATUS = 401;

// 8s cap on the /auth/me probe. Why: /auth/me is a fast cookie check; anything
// longer is a real outage (backend down / surreal down / network), and an infinite
// spinner gives the member no signal that something is wrong. Aborting the fetch
// frees the network resource and routes to the `error` phase (retryable).
const PROBE_TIMEOUT_MS = 8000;

// No cross-mount memo of the verdict (part C's compensation, deleted): a memo
// existed only because the old top-level sibling split REMOUNTED the decider on
// every /projects/:id ↔ /docs/:id crossing, and softening that remount beat fixing
// it. SessionRoute now stays mounted across crossings; it unmounts only for `/`
// and `/s/:token`, a full page transition where a fresh probe is the honest cost.
// Caching a session verdict past its response would keep a cached security
// decision alive for zero remaining benefit.

// data-testid: this spinner's markup is shared with the lazy-chunk Suspense
// fallbacks, so the cross-branch e2e frame probe has to be able to count THIS one
// specifically.
function RouteLoading() {
  return (
    <div data-testid="route-loading" className="flex h-screen items-center justify-center bg-bg text-text">
      <div className="w-6 h-6 border-2 border-text-dim border-t-transparent animate-spin" />
    </div>
  );
}

/** The probe's verdict. `authed` CARRIES the user rather than pairing with a
 *  separate state slot: an authed phase without a user is not a state this
 *  component can be in, and a discriminated union is what makes it untypable.
 *  Why: with the two split, the `authed && user` render guard could fall through
 *  to the anonymous branch and redirect a signed-in member to `/` — silent
 *  degradation, the exact failure this file's ARCH forbids. */
type Phase =
  | { kind: 'probing' }
  | { kind: 'authed'; user: User }
  | { kind: 'anonymous' }
  | { kind: 'error' };

/** The anonymous branch's ONE public surface: /docs/:documentId → the read-only
 * published view (or PublicSharePage's error state if the document is not
 * published). documentId drives the document-keyed public bundle; unknown /
 * unshared ids 404 uniformly inside resolve_share. */
function PublicDocRoute() {
  const { documentId } = useParams<{ documentId: string }>();
  // Defensive: the /docs/:documentId route always provides the param, so this is
  // never hit in practice — a loading state (not a 404) avoids second-guessing the
  // resolver for an unknown id format.
  if (!documentId) return <RouteLoading />;
  return (
    <Suspense fallback={<RouteLoading />}>
      <PublicSharePage documentId={documentId} />
    </Suspense>
  );
}

export function SessionRoute() {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<Phase>({ kind: 'probing' });
  // Bumping retryKey re-runs the probe effect (the `error` phase's Retry button).
  const [retryKey, setRetryKey] = useState(0);

  // Probe the session. Runs on mount and on each retry (retryKey). The probe is
  // bounded by PROBE_TIMEOUT_MS via an AbortController: on timeout the fetch is
  // aborted and rejects → error phase. Cleanup aborts an in-flight probe on
  // unmount so a late resolution never calls setState on an unmounted component.
  useEffect(() => {
    let settled = false;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);

    apiClient.probe('/auth/me', controller.signal)
      .then((r) => {
        if (settled) return;
        // A 200 whose body did not parse is NOT a verified session: the guard's
        // contract is a real user, and inventing one would be exactly the silent
        // degradation the error phase exists to prevent.
        const verdict: Phase = r.ok
          ? (r.data ? { kind: 'authed', user: r.data as User } : { kind: 'error' })
          : r.status === ANONYMOUS_STATUS ? { kind: 'anonymous' } : { kind: 'error' };
        setPhase(verdict);
      })
      .catch(() => {
        // AbortError (timeout/unmount), TypeError (network), or any other throw —
        // none of these mean "anonymous"; route to the explicit error phase.
        if (settled) return;
        setPhase({ kind: 'error' });
      })
      .finally(() => {
        settled = true;
        window.clearTimeout(timeoutId);
      });

    return () => {
      settled = true;
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [retryKey]);

  function retry() {
    setPhase({ kind: 'probing' });
    setRetryKey(k => k + 1);
  }

  if (phase.kind === 'probing') return <RouteLoading />;

  if (phase.kind === 'error') {
    // Couldn't verify the session — a transient backend/network/timeout failure.
    // Explicit state (distinct from the loading spinner and from the public page)
    // with a Retry that re-probes. Mirrors the no-silent-degradation rule: never
    // show the read-only surface when we simply couldn't determine the audience.
    return (
      <div className="h-screen flex flex-col items-center justify-center gap-4 bg-bg text-text">
        <p className="text-text-dim">{t('publicShareSessionProbeFailed')}</p>
        <Button variant="subtle" type="button" onClick={retry}>{t('retry')}</Button>
      </div>
    );
  }

  if (phase.kind === 'authed') {
    // Full session/prefs/refresh parity for EVERY authed surface, /docs/:id
    // included: AuthGuard bootstraps the session from the user THIS probe already
    // fetched (no second /auth/me, no 401-redirect risk), and Layout gates on
    // pinLocked, which the old top-level /docs/:id sibling bypassed entirely.
    return (
      <AuthGuard user={phase.user}>
        <Layout>
          <Routes>
            <Route path="/projects" element={<Suspense fallback={<RouteLoading />}><Dashboard /></Suspense>} />
            <Route path="/cabinet" element={<Suspense fallback={null}><CabinetPage /></Suspense>} />
            <Route path="/admin" element={<Suspense fallback={null}><AdminPage /></Suspense>} />
            {/* Everything else is the editor: one chunk (ProjectPage +
                DocumentPage statically linked), mounted behind ONE route element
                so /projects/:id ↔ /docs/:id crossings are inner route changes
                with no chunk boundary to suspend on. */}
            <Route path="*" element={<Suspense fallback={<RouteLoading />}><EditorRoutes /></Suspense>} />
          </Routes>
        </Layout>
      </AuthGuard>
    );
  }

  // Anonymous (401): /docs/:documentId is the one public surface; everything
  // else redirects to the landing page CLIENT-SIDE (a Navigate, not the
  // apiClient 401 hard-redirect — the probe already classified the visitor).
  // `/` and `/s/:token` outrank /* at the top level, so this catch-all can
  // never swallow the published-link entry flow.
  return (
    <Routes>
      <Route path="/docs/:documentId" element={<PublicDocRoute />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
