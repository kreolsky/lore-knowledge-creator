/** Layout — the PIN-lock gate and the shell switch.
 *
 * Pins the security-relevant consequence of plan "route-unification-single-shell":
 * /docs/:id — the CANONICAL editor URL — renders through Layout, so the PIN gate
 * covers it. Before the unification that route bypassed Layout entirely and the
 * lock never fired on it (see lessons/2026-03-20-security-review-pin-lock.md:
 * a lock screen must REPLACE the tree, never overlay it).
 *
 * Harness: manual createRoot + act, mirroring ProjectPage.test.tsx. react-router
 * -dom is mocked down to useLocation (Layout reads nothing else from the router).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let Layout: typeof import('./Layout').Layout;
let container: HTMLDivElement;
let root: Root;
let appState: Record<string, unknown>;
let pathname: string;

const CHILD_MARK = 'layout-children';

function Child() { return createElement('p', null, CHILD_MARK); }
function pinLockMarker() { return createElement('div', { 'data-testid': 'pin-lock' }); }
function headerMarker() { return createElement('div', { 'data-testid': 'header' }); }
function navBarMarker() { return createElement('div', { 'data-testid': 'navbar' }); }

beforeEach(async () => {
  vi.resetModules();
  appState = { pinLocked: false };
  pathname = '/';

  vi.doMock('react-router-dom', () => ({
    useLocation: () => ({ pathname }),
  }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: (sel: (s: Record<string, unknown>) => unknown) => sel(appState),
  }));
  vi.doMock('./PinLockScreen', () => ({ PinLockScreen: pinLockMarker }));
  vi.doMock('./Header', () => ({ Header: headerMarker }));
  vi.doMock('./NavigationTabBar', () => ({ NavigationTabBar: navBarMarker }));

  ({ Layout } = await import('./Layout'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('./PinLockScreen');
  vi.doUnmock('./Header');
  vi.doUnmock('./NavigationTabBar');
});

function render(children: ReactNode) {
  act(() => root.render(createElement(Layout, null, children)));
}

describe('Layout — PIN lock covers the canonical editor URL', () => {
  it('pinLocked on /docs/:id renders PinLockScreen and NOT the protected content', () => {
    pathname = '/docs/012b3cb2-a5f7-49c6-9038-fe1d7d787199';
    appState.pinLocked = true;
    render(createElement(Child));
    expect(container.querySelector('[data-testid="pin-lock"]')).not.toBeNull();
    // REPLACE, never overlay: the children must not be in the DOM at all.
    expect(container.textContent).not.toContain(CHILD_MARK);
  });

  it('pinLocked on /projects/:id renders PinLockScreen too (parity with the legacy URL)', () => {
    pathname = '/projects/p1';
    appState.pinLocked = true;
    render(createElement(Child));
    expect(container.querySelector('[data-testid="pin-lock"]')).not.toBeNull();
    expect(container.textContent).not.toContain(CHILD_MARK);
  });

  it('unlocked /docs/:id renders the chromeless editor shell with the children', () => {
    pathname = '/docs/012b3cb2-a5f7-49c6-9038-fe1d7d787199';
    render(createElement(Child));
    expect(container.querySelector('[data-testid="pin-lock"]')).toBeNull();
    expect(container.querySelector('[data-testid="header"]')).toBeNull();
    expect(container.querySelector('[data-testid="navbar"]')).toBeNull();
    expect(container.textContent).toContain(CHILD_MARK);
  });

  it('unlocked /projects list renders the framed shell (Header + NavigationTabBar)', () => {
    pathname = '/projects';
    render(createElement(Child));
    expect(container.querySelector('[data-testid="header"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="navbar"]')).not.toBeNull();
    expect(container.textContent).toContain(CHILD_MARK);
  });

  // Section pages (/admin, /cabinet) mount their own SectionShell with its own
  // left tab bar — a second bar from Layout would double the chrome.
  it('unlocked /cabinet renders the frame WITHOUT NavigationTabBar', () => {
    pathname = '/cabinet';
    render(createElement(Child));
    expect(container.querySelector('[data-testid="header"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="navbar"]')).toBeNull();
    expect(container.textContent).toContain(CHILD_MARK);
  });

  it('unlocked /admin renders the frame WITHOUT NavigationTabBar', () => {
    pathname = '/admin';
    render(createElement(Child));
    expect(container.querySelector('[data-testid="header"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="navbar"]')).toBeNull();
    expect(container.textContent).toContain(CHILD_MARK);
  });
});
