/** TDD: defines the contract for useDocumentRoute before implementation.
 *
 * useDocumentRoute() returns { projectId, documentId } for BOTH URL shapes:
 *   - /projects/:projectId/docs/:documentId  → both from route params
 *   - /docs/:documentId                       → documentId from route param,
 *     projectId from the store (currentProject.project_id), populated by the
 *     /open bootstrap on the bare-URL cold path (plan "public-document-ids").
 *
 * Pins both paths so a legacy useParams mock can't hide a broken /docs/:id
 * route (false-green rule): a component test must not keep returning projectId
 * after the real route stops providing it.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

let useDocumentRoute: typeof import('./useDocumentRoute').useDocumentRoute;
let container: HTMLDivElement;
let root: Root;

let useParamsMock: () => Record<string, string | undefined>;
let storeProjectId: string | null;

/** Minimal renderHook (no @testing-library/react): runs fn inside a Probe and
 *  stashes the return value. */
function renderHook<T>(fn: () => T): { current: T } {
  const box: { current: T } = { current: undefined as unknown as T };
  function Probe() {
    box.current = fn();
    return null;
  }
  act(() => root.render(createElement(Probe)));
  return box;
}

beforeEach(async () => {
  vi.resetModules();
  useParamsMock = () => ({});
  storeProjectId = null;

  vi.doMock('react-router-dom', () => ({ useParams: () => useParamsMock() }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: (selector: (s: { currentProject: { project_id: string } | null }) => unknown) =>
      selector({ currentProject: storeProjectId ? { project_id: storeProjectId } : null }),
  }));

  ({ useDocumentRoute } = await import('./useDocumentRoute'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../store/app-store');
});

describe('useDocumentRoute', () => {
  it('returns both ids from the /projects/:projectId/docs/:documentId route', () => {
    useParamsMock = () => ({ projectId: 'proj-1', documentId: 'doc-1' });
    const { current } = renderHook(() => useDocumentRoute());
    expect(current).toEqual({ projectId: 'proj-1', documentId: 'doc-1' });
  });

  it('on /docs/:documentId, resolves projectId from the store (currentProject)', () => {
    // The bare route provides documentId only — projectId is absent. The /open
    // bootstrap populates currentProject; useDocumentRoute must fall back to it.
    useParamsMock = () => ({ documentId: 'doc-1' });
    storeProjectId = 'proj-from-store';
    const { current } = renderHook(() => useDocumentRoute());
    expect(current).toEqual({ projectId: 'proj-from-store', documentId: 'doc-1' });
  });

  it('route projectId wins over the store when both are present', () => {
    useParamsMock = () => ({ projectId: 'route-proj', documentId: 'd' });
    storeProjectId = 'store-proj';
    const { current } = renderHook(() => useDocumentRoute());
    expect(current.projectId).toBe('route-proj');
  });

  it('returns undefined projectId when neither route nor store provides one', () => {
    // Public share (/s/:token) and the bare cold path before /open resolves.
    useParamsMock = () => ({ documentId: 'd' });
    storeProjectId = null;
    const { current } = renderHook(() => useDocumentRoute());
    expect(current.projectId).toBeUndefined();
    expect(current.documentId).toBe('d');
  });
});
