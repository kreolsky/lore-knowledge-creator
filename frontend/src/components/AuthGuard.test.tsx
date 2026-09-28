/** AuthGuard — bootstraps an ALREADY-verified session.
 *
 * Pins the fold of the double /auth/me: SessionRoute's probe is the ONE session
 * fetch, and the guard consumes its parsed user through a required prop. What is
 * asserted here is exactly what regressing would restore — a second /auth/me on
 * every cold authed load, serial behind the probe.
 *
 * Harness: manual createRoot + act, mirroring Layout.test.tsx.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { User } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let AuthGuard: typeof import('./AuthGuard').AuthGuard;
let container: HTMLDivElement;
let root: Root;

let getMock: ReturnType<typeof vi.fn>;
let putMock: ReturnType<typeof vi.fn>;
let setCurrentUser: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let startSessionRefresh: ReturnType<typeof vi.fn>;
let stopRefresh: ReturnType<typeof vi.fn>;
/** loadGlobalPrefs is held open so "children wait for prefs" has a failing branch. */
let releasePrefs!: () => void;
let prefsCalls: number;

const CHILD_MARK = 'guard-children';
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

function user(extra: Partial<User> = {}): User {
  return {
    user_id: 'u1', name: 'Red', email: 'red@lore.app', role: 'user',
    has_pin: false, user_facts: '', timezone: TZ, ...extra,
  };
}

function render(u: User) {
  act(() => root.render(createElement(AuthGuard, {
    user: u,
    children: createElement('p', null, CHILD_MARK),
  })));
}

async function flush() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)); });
}

beforeEach(async () => {
  vi.resetModules();
  prefsCalls = 0;
  getMock = vi.fn(async () => ({}));
  putMock = vi.fn(async () => ({}));
  setCurrentUser = vi.fn();
  showToast = vi.fn();
  stopRefresh = vi.fn();
  startSessionRefresh = vi.fn(() => stopRefresh);

  const appState = { setCurrentUser, showToast };
  vi.doMock('../api/client', () => ({ apiClient: { get: getMock, put: putMock } }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (sel: (s: typeof appState) => unknown) => sel(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('../store/ui-store', () => ({
    useUIStore: {
      getState: () => ({
        loadGlobalPrefs: () => {
          prefsCalls += 1;
          return new Promise<void>((res) => { releasePrefs = res; });
        },
      }),
    },
  }));
  vi.doMock('../hooks/useSessionRefresh', () => ({ startSessionRefresh }));
  vi.doMock('../i18n', () => ({ t: (k: string) => k }));

  ({ AuthGuard } = await import('./AuthGuard'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../api/client');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
  vi.doUnmock('../hooks/useSessionRefresh');
  vi.doUnmock('../i18n');
});

describe('AuthGuard — the verified user comes in as a prop', () => {
  it('never fetches /auth/me itself', async () => {
    render(user());
    await flush();
    act(() => { releasePrefs(); });
    await flush();
    const meCalls = getMock.mock.calls.filter(([endpoint]) => endpoint === '/auth/me');
    expect(meCalls).toEqual([]);
  });

  it('seeds the store from the prop', async () => {
    const u = user();
    render(u);
    await flush();
    expect(setCurrentUser).toHaveBeenCalledWith(u);
  });

  it('children mount only after loadGlobalPrefs resolves', async () => {
    render(user());
    await flush();
    expect(prefsCalls).toBeGreaterThan(0);
    // Prefs still pending → nothing rendered (no unstyled flash).
    expect(container.textContent).not.toContain(CHILD_MARK);
    act(() => { releasePrefs(); });
    await flush();
    expect(container.textContent).toContain(CHILD_MARK);
  });

  it('starts the refresh scheduler with skipInitialRefresh — the probe already slid the window', async () => {
    render(user());
    await flush();
    act(() => { releasePrefs(); });
    await flush();
    expect(startSessionRefresh).toHaveBeenCalled();
    expect(startSessionRefresh.mock.calls[0][0]).toMatchObject({ skipInitialRefresh: true });
  });

  it('syncs a drifted timezone from the prop user', async () => {
    render(user({ timezone: 'Antarctica/Troll' }));
    await flush();
    act(() => { releasePrefs(); });
    await flush();
    expect(putMock).toHaveBeenCalledWith('/users/u1', { timezone: TZ });
  });

  it('does not sync when the timezone already matches', async () => {
    render(user());
    await flush();
    act(() => { releasePrefs(); });
    await flush();
    expect(putMock).not.toHaveBeenCalled();
  });

  it('stops the refresh scheduler on unmount', async () => {
    render(user());
    await flush();
    act(() => { releasePrefs(); });
    await flush();
    act(() => root.render(createElement('div')));
    expect(stopRefresh).toHaveBeenCalled();
  });
});
