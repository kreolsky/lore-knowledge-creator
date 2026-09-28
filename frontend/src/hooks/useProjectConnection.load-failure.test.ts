/**
 * Dead-button regression: a failed project load in
 * useProjectConnection used to silently bounce the user to '/' (console.error
 * only). Contract pinned here — the toast-then-redirect twin of DocumentPage's
 * documentOpenFailed handler: the projectLoadFailed error toast fires BEFORE
 * the navigate('/'), so the user sees WHY they were bounced.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let useProjectConnection: typeof import('./useProjectConnection').useProjectConnection;
let getMock: ReturnType<typeof vi.fn>;
let navigateMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let order: string[];
let clearProjectMirrors: ReturnType<typeof vi.fn<(projectId: string) => void>>;

beforeEach(async () => {
  vi.resetModules();
  getMock = vi.fn();
  order = [];
  navigateMock = vi.fn(() => { order.push('navigate'); });
  showToast = vi.fn(() => { order.push('toast'); });

  vi.doMock('react-router-dom', () => ({ useNavigate: () => navigateMock }));
  clearProjectMirrors = vi.fn<(projectId: string) => void>();
  vi.doMock('../api/client', async () => {
    // The real error taxonomy — the access verdict is decided by isAccessRefusal over
    // real ForbiddenError/HttpError instances, so a stub class would test nothing.
    const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
    return { ...actual, apiClient: { get: getMock, post: vi.fn(), patch: vi.fn(), delete: vi.fn() } };
  });
  vi.doMock('../collab/local-doc-persistence', () => ({
    clearProjectLocalDocs: (projectId: string) => clearProjectMirrors(projectId),
  }));
  vi.doMock('../collab/project-connection', () => ({
    ProjectConnection: class { connect = vi.fn(); disconnect = vi.fn(); },
  }));
  vi.doMock('../collab/yjs-provider', () => ({
    YjsProjectProvider: class { connect = vi.fn(); disconnect = vi.fn(); },
  }));
  vi.doMock('../events', () => ({ emit: vi.fn() }));
  vi.doMock('./useEvent', () => ({ useEvent: () => {} }));
  vi.doMock('../i18n', () => ({ t: (k: string) => k, useTranslation: () => ({ t: (k: string) => k }) }));

  const appState = {
    showToast,
    setCurrentProject: vi.fn(),
    setAccessLevel: vi.fn(),
    currentUser: { user_id: 'u1' },
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      // chat-store subscribes to app-store at module load; the hook imports it.
      { getState: () => appState, subscribe: () => () => {} },
    ),
  }));

  ({ useProjectConnection } = await import('./useProjectConnection'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { try { root.unmount(); } catch { /* already unmounted by a test */ } });
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../collab/local-doc-persistence');
  vi.doUnmock('../collab/project-connection');
  vi.doUnmock('../collab/yjs-provider');
  vi.doUnmock('../events');
  vi.doUnmock('./useEvent');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
});

describe('useProjectConnection — project load failure toasts before the bounce', () => {
  it('a rejecting GET /projects/:id shows the projectLoadFailed error toast, THEN navigates to /', async () => {
    getMock.mockRejectedValue(new Error('boom'));
    function Harness() {
      useProjectConnection({ projectId: 'p1' });
      return null;
    }
    await act(async () => { root.render(createElement(Harness)); });

    expect(showToast).toHaveBeenCalledWith('projectLoadFailed', 'error');
    expect(navigateMock).toHaveBeenCalledWith('/');
    expect(order).toEqual(['toast', 'navigate']);
  });

  it('a rejection landing AFTER unmount neither toasts nor bounces', async () => {
    let reject!: (e: Error) => void;
    getMock.mockReturnValue(new Promise((_, rej) => { reject = rej; }));
    function Harness() {
      useProjectConnection({ projectId: 'p1' });
      return null;
    }
    await act(async () => { root.render(createElement(Harness)); });

    await act(async () => { root.unmount(); });
    await act(async () => { reject(new Error('boom')); await Promise.resolve(); });

    expect(showToast).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it('a 404 on the project deletes that project\'s local mirrors', async () => {
    const { HttpError } = await import('../api/client');
    getMock.mockRejectedValue(new HttpError(404, 'Project not found'));
    function Harness() {
      useProjectConnection({ projectId: 'p1' });
      return null;
    }
    await act(async () => { root.render(createElement(Harness)); });

    expect(clearProjectMirrors).toHaveBeenCalledWith('p1');
  });

  it('a 403 on the project deletes that project\'s local mirrors', async () => {
    // The revocation case: apiClient turns 403 into ForbiddenError, which carries no
    // `status` at all — the reason this goes through isAccessRefusal.
    const { ForbiddenError } = await import('../api/client');
    getMock.mockRejectedValue(new ForbiddenError());
    function Harness() {
      useProjectConnection({ projectId: 'p1' });
      return null;
    }
    await act(async () => { root.render(createElement(Harness)); });

    expect(clearProjectMirrors).toHaveBeenCalledWith('p1');
  });

  it('an OUTAGE leaves the mirrors alone — that is what they exist for', async () => {
    const { HttpError } = await import('../api/client');
    getMock.mockRejectedValue(new HttpError(503, 'Service Unavailable'));
    function Harness() {
      useProjectConnection({ projectId: 'p1' });
      return null;
    }
    await act(async () => { root.render(createElement(Harness)); });

    expect(clearProjectMirrors).not.toHaveBeenCalled();
  });

});
