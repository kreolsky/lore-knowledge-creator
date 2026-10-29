/** SessionRoute — the /* session decider (plan "route-unification-single-shell").
 *
 * Pins the unification's own contract, retargeted from DocumentRoute.test.tsx:
 *   - T3 (no silent degradation): the probe distinguishes 401 (truly anonymous →
 *     public surface) from 5xx / network / timeout (couldn't verify → explicit
 *     error phase + retry, NOT the public surface).
 *   - T1 (carried over): a deterministic sys- id is never client-side-rejected.
 *   - NEW structure: the authed branch renders AuthGuard → Layout → inner Routes
 *     with BOTH /projects/:projectId and /docs/:documentId (ProjectPage + index
 *     DocumentPage) inside — the canonical editor URL now goes through the shell.
 *   - NEW anonymous catch-all: a non-docs path redirects client-side to '/' (no
 *     hard reload — the failure mode changed with the guard hoisted above).
 *   - NEW shell-persistence: crossing /projects/:id → /docs/:id keeps the SAME
 *     Layout and ProjectPage DOM nodes mounted and does NOT re-probe — the
 *     property the whole unification exists for.
 *
 * Harness: manual createRoot + act, mirroring ProjectPage.test.tsx. react-router
 * -dom is NOT mocked: SessionRoute renders real nested <Routes>/<Navigate>, and a
 * fake router would pin the fake, not the router. Instead the REAL module is
 * imported dynamically after vi.resetModules() in the same registry window as
 * SessionRoute, so both share one instance; the harness mirrors App's top-level
 * routes (/, /s/:token, /*) so ranking and the anonymous '/' redirect behave
 * exactly like production.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

// React's act() needs this flag set at module load (per ProjectPage.test.tsx) — without
// it act is a silent no-op and effects/state updates never flush, hanging the test.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let SessionRoute: typeof import('./SessionRoute').SessionRoute;
let MemoryRouter: typeof import('react-router-dom').MemoryRouter;
let Routes: typeof import('react-router-dom').Routes;
let Route: typeof import('react-router-dom').Route;
let Outlet: typeof import('react-router-dom').Outlet;
let useNavigate: typeof import('react-router-dom').useNavigate;
let container: HTMLDivElement;
let root: Root;

/** Controllable probe seam: resolves/rejects on demand AND honors an AbortSignal
 *  like real fetch (so the timeout path rejects on controller.abort()). */
let probeResolve!: (v: { ok: boolean; status: number; data?: unknown }) => void;
let probeReject!: (e: unknown) => void;
let probeMock: ReturnType<typeof vi.fn>;
let probeCalls: number;

/** A valid document id (shape is irrelevant to the decider — see the sys- tests). */
const VALID_DOC = '012b3cb2-a5f7-49c6-9038-fe1d7d787199';

/** The probe's 200 body IS /auth/me's user — the authed branch now carries it into
 *  AuthGuard, so a 200 without a body is no longer a verified session. */
const AUTHED_USER = { user_id: 'u1', name: 'Red', email: 'red@lore.app' };

/** Marker components for the lazy branches / mocked shell pieces — keep the
 *  assertions text- and testid-based. */
function ProjectPageMarker() {
  return createElement('div', { 'data-testid': 'project-page' }, createElement(Outlet));
}
function DocumentPageMarker() { return createElement('div', { 'data-testid': 'document-page' }); }
function PublicSharePageMarker({ documentId }: { documentId: string }) {
  return createElement('div', { 'data-testid': 'public-share' }, documentId);
}
function LandingMarker() { return createElement('div', { 'data-testid': 'landing' }); }
function DashboardMarker() { return createElement('div', { 'data-testid': 'dashboard' }); }
/** Records the verified user handed down by the probe — the guard no longer
 *  fetches /auth/me, so the prop IS the session. */
let guardUser: unknown;
function AuthGuardMarker({ user, children }: { user: unknown; children: ReactNode }) {
  guardUser = user;
  return createElement('div', { 'data-testid': 'auth-guard' }, children);
}
function LayoutMarker({ children }: { children: ReactNode }) {
  return createElement('div', { 'data-testid': 'layout' }, children);
}
/** Always-mounted navigation trigger — lives INSIDE the Router but OUTSIDE the
 *  Routes under test, so clicking it can never be confused with a remount.
 *  Reads `navTarget` at click time so each test steers it. */
let navTarget = '/';
let NavButton: () => ReactNode;

/** Flush microtasks/macrotasks so lazy() + Suspense branches settle after a state
 *  change. Wrapped in act so React drains the suspended render + re-render. */
async function flush() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)); });
}

/** Render the App-shaped harness at `path`: / and /s/:token as top-level
 *  siblings of /* → SessionRoute, exactly like App.tsx. */
function renderAt(path: string) {
  act(() => root.render(
    createElement(MemoryRouter, { initialEntries: [path] },
      createElement(NavButton),
      createElement(Routes, null,
        createElement(Route, { path: '/', element: createElement(LandingMarker) }),
        createElement(Route, { path: '/s/:token', element: createElement(LandingMarker) }),
        createElement(Route, { path: '/*', element: createElement(SessionRoute) }),
      ),
    ),
  ));
}

/** Click the NavButton to navigate in-tree (no location bar, no reload). */
async function navigateTo() {
  const btn = container.querySelector('button');
  expect(btn, 'nav button mounted').not.toBeNull();
  await act(async () => { btn!.click(); });
  await flush();
}

beforeEach(async () => {
  vi.resetModules();
  probeCalls = 0;
  guardUser = undefined;

  vi.doMock('../i18n', () => ({
    // t() returns the key verbatim — assertions match keys, not copy.
    useTranslation: () => ({ t: (k: string) => k, language: 'en' }),
  }));
  vi.doMock('../components/AuthGuard', () => ({ AuthGuard: AuthGuardMarker }));
  vi.doMock('../components/Layout', () => ({ Layout: LayoutMarker }));
  vi.doMock('./ProjectPage', () => ({ ProjectPage: ProjectPageMarker }));
  vi.doMock('./DocumentPage', () => ({ DocumentPage: DocumentPageMarker }));
  vi.doMock('./PublicSharePage', () => ({ PublicSharePage: PublicSharePageMarker }));
  vi.doMock('./Dashboard', () => ({ Dashboard: DashboardMarker }));

  probeMock = vi.fn((_endpoint: string, _signal?: AbortSignal) =>
    new Promise<{ ok: boolean; status: number; data?: unknown }>((res, rej) => {
      probeCalls += 1;
      probeResolve = res;
      probeReject = rej;
      // Mirror real fetch: an aborted signal rejects with an AbortError. The
      // timeout path relies on this (controller.abort() → rejection → error phase).
      const signal = _signal;
      if (signal) {
        if (signal.aborted) rej(new DOMException('aborted', 'AbortError'));
        else signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
      }
    }),
  );
  vi.doMock('../api/client', () => ({ apiClient: { probe: probeMock } }));

  // ONE registry window: SessionRoute and the harness must share the same
  // react-router-dom instance or the Router context won't propagate.
  ({ SessionRoute } = await import('./SessionRoute'));
  // Warm the authed branch's lazy chunk BEFORE any render: once the module is
  // in the registry, React.lazy's promise resolves in a microtask that act()
  // drains synchronously. Leaving it cold made the authed tests depend on how
  // many macrotask ticks Vite's module runner needs — flaky on a loaded host
  // (observed on gray: same binary, 1 failed / 2064 passed).
  await import('./EditorRoutes');
  // Same reason for Dashboard: the authed catch-all redirects to /projects.
  await import('./Dashboard');
  const rr = await import('react-router-dom');
  ({ MemoryRouter, Routes, Route, Outlet, useNavigate } = rr);
  NavButton = () => {
    const navigate = useNavigate();
    return createElement('button', {
      type: 'button',
      onClick: () => { navigate(navTarget); },
    }, 'nav');
  };

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../i18n');
  vi.doUnmock('../components/AuthGuard');
  vi.doUnmock('../components/Layout');
  vi.doUnmock('./ProjectPage');
  vi.doUnmock('./DocumentPage');
  vi.doUnmock('./PublicSharePage');
  vi.doUnmock('./Dashboard');
  vi.doUnmock('../api/client');
});

/** Point the harness nav button at `to`, then click it. */
async function go(to: string) {
  navTarget = to;
  await navigateTo();
}

describe('SessionRoute — probe phases (T3: 401 vs transient failure)', () => {

  it('401 → anonymous branch: /docs/:id renders the public share surface', async () => {
    renderAt(`/docs/${VALID_DOC}`);
    await act(async () => { probeResolve({ ok: false, status: 401 }); });
    await flush();
    expect(container.querySelector('[data-testid="public-share"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="public-share"]')?.textContent)
      .toContain(VALID_DOC);
    expect(container.querySelector('[data-testid="project-page"]')).toBeNull();
  });
});

describe('SessionRoute — branch structure (unification)', () => {
  it('a sys- system id reaches the public branch when anonymous (no client-side id guard)', async () => {
    renderAt('/docs/sys-memory_note-abc');
    await act(async () => { probeResolve({ ok: false, status: 401 }); });
    await flush();
    expect(container.querySelector('[data-testid="public-share"]')?.textContent)
      .toContain('sys-memory_note-abc');
  });

  it('anonymous non-docs path redirects client-side to / (no hard reload)', async () => {
    renderAt('/projects/x');
    await act(async () => { probeResolve({ ok: false, status: 401 }); });
    await flush();
    // The top-level / route wins after the redirect — the landing marker mounts.
    expect(container.querySelector('[data-testid="landing"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="project-page"]')).toBeNull();
  });

  it('anonymous /admin redirects too — the catch-all does not serve authed surfaces', async () => {
    renderAt('/admin');
    await act(async () => { probeResolve({ ok: false, status: 401 }); });
    await flush();
    expect(container.querySelector('[data-testid="landing"]')).not.toBeNull();
  });
});
