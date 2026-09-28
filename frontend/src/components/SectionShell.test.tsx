/** SectionShell — the /admin + /cabinet layout chrome.
 *
 * Pins the section-shell plan's origin contract:
 * - the aside's back button shows the stored sectionOrigin label and navigates
 *   to its path;
 * - a null origin degrades to /projects + "Projects" (never a dead button);
 * - tab parity with ProjectShell: clicking the active tab collapses the aside,
 *   re-clicking any tab reopens it;
 * - a fromProject origin renders ONE inherited tab above the section tabs —
 *   the project's Documents icon, navigating to origin.path, never `active`.
 *
 * Harness: manual createRoot + act, mirroring Layout.test.tsx. The router is
 * mocked down to useNavigate (captured) + useLocation; app-store down to a
 * selector over a plain object.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let SectionShell: typeof import('./SectionShell').SectionShell;
let container: HTMLDivElement;
let root: Root;
let appState: Record<string, unknown>;
let navigate: ReturnType<typeof vi.fn>;

const I18N: Record<string, string> = { projects: 'Projects', tabDocuments: 'Documents' };
const tFn = (k: string) => I18N[k] ?? k;

function userControlsMarker() { return createElement('div', { 'data-testid': 'user-controls' }); }

const PANEL_ONE = 'panel-one';
const PANEL_TWO = 'panel-two';

function tabs() {
  return [
    { tab: 'one', icon: createElement('i'), title: 'One', renderPanel: () => PANEL_ONE },
    { tab: 'two', icon: createElement('i'), title: 'Two', renderPanel: () => PANEL_TWO },
  ];
}

beforeEach(async () => {
  vi.resetModules();
  appState = { sectionOrigin: null };
  navigate = vi.fn();

  vi.doMock('react-router-dom', () => ({
    useNavigate: () => navigate,
    useLocation: () => ({ pathname: '/cabinet' }),
  }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: (sel: (s: Record<string, unknown>) => unknown) => sel(appState),
  }));
  vi.doMock('./UserControls', () => ({ UserControls: userControlsMarker }));
  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: tFn }),
  }));

  ({ SectionShell } = await import('./SectionShell'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('./UserControls');
  vi.doUnmock('../i18n');
});

function render(children: ReactNode) {
  act(() => root.render(children));
}

function shell() {
  return createElement(SectionShell, { tabs: tabs(), renderCenter: () => 'center' });
}

/** The aside's back button: the only dashed button in the shell. */
function backButton() {
  return Array.from(container.querySelectorAll('button'))
    .find(b => b.className.includes('border-dashed'));
}

function tabButton(title: string) {
  return Array.from(container.querySelectorAll('button'))
    .find(b => b.getAttribute('title') === title);
}

function aside() {
  return container.querySelector('aside');
}

/** All buttons wearing the left-rail tab chrome (in DOM order). UserControls
 *  is mocked to a marker div, so this is exactly the rail's tab column. */
function leftBarTabs() {
  return Array.from(container.querySelectorAll<HTMLButtonElement>('button.left-bar-tab'));
}

/** jsdom has no PointerEvent; dispatch MouseEvent with pointer-typed names —
 * the handlers only read clientX/target (useResizer.test.ts pattern). */
function pointer(type: string, target: EventTarget, clientX: number) {
  act(() => {
    target.dispatchEvent(new MouseEvent(type, { clientX, bubbles: true }));
  });
}

describe('SectionShell — origin back button', () => {
  it('shows the stored origin label and navigates to the origin path', () => {
    appState.sectionOrigin = { path: '/docs/abc', label: 'The Document', fromProject: false };
    render(shell());
    expect(backButton()?.textContent).toContain('The Document');
    act(() => backButton()?.click());
    expect(navigate).toHaveBeenCalledWith('/docs/abc');
  });

  it('null origin degrades to /projects + "Projects"', () => {
    appState.sectionOrigin = null;
    render(shell());
    expect(backButton()?.textContent).toContain('Projects');
    act(() => backButton()?.click());
    expect(navigate).toHaveBeenCalledWith('/projects');
    // No origin → no inherited tab either (a cold reload of /admin has none).
    expect(leftBarTabs().length).toBe(2); // the two section tabs only
  });
});

describe('SectionShell — inherited project Documents tab', () => {
  it('fromProject renders one Documents tab first; click navigates to origin.path', () => {
    appState.sectionOrigin = { path: '/p/1/doc/9', label: 'Open doc', fromProject: true };
    render(shell());
    const barTabs = leftBarTabs();
    expect(barTabs.length).toBe(3); // inherited tab + two section tabs
    expect(barTabs[0].getAttribute('title')).toBe('Documents');
    act(() => barTabs[0].click());
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/p/1/doc/9');
  });

  it('fromProject false → no inherited tab when tabs=[]', () => {
    appState.sectionOrigin = { path: '/projects', label: 'My projects', fromProject: false };
    render(createElement(SectionShell, { tabs: [], renderCenter: () => 'center' }));
    expect(leftBarTabs().length).toBe(0);
  });

  it('the inherited tab is never active, however section tabs are clicked', () => {
    appState.sectionOrigin = { path: '/p/1/doc/9', label: 'Open doc', fromProject: true };
    render(shell());
    act(() => tabButton('Two')?.click());
    const inherited = leftBarTabs()[0];
    expect(inherited.className).not.toContain('active');
    expect(tabButton('Two')?.className).toContain('active');
    act(() => inherited.click());
    expect(inherited.className).not.toContain('active');
  });
});

describe('SectionShell — tab parity with ProjectShell', () => {
  it('first tab is active; clicking the other tab switches the panel', () => {
    render(shell());
    expect(container.textContent).toContain(PANEL_ONE);
    act(() => tabButton('Two')?.click());
    expect(container.textContent).toContain(PANEL_TWO);
    expect(container.textContent).not.toContain(PANEL_ONE);
  });

  it('clicking the active tab collapses the aside; clicking any tab reopens it', () => {
    render(shell());
    expect(aside()?.style.width).not.toBe('0px');
    act(() => tabButton('One')?.click());
    expect(aside()?.style.width).toBe('0px');
    act(() => tabButton('Two')?.click());
    expect(aside()?.style.width).not.toBe('0px');
    // The back button is reachable again after a snap-close reopen.
    expect(backButton()).toBeDefined();
  });
});

describe('SectionShell — opt-in aside-width persistence props', () => {
  it('initialAsideWidth seeds the aside width; without it the 220 default renders', () => {
    render(createElement(SectionShell, {
      tabs: tabs(), renderCenter: () => 'center', initialAsideWidth: 333,
    }));
    expect(aside()?.style.width).toBe('333px');
  });

  it('a drag reports the final width via onAsideWidthChange (clamped to max 500)', () => {
    const onAsideWidthChange = vi.fn();
    render(createElement(SectionShell, {
      tabs: tabs(), renderCenter: () => 'center',
      initialAsideWidth: 220, onAsideWidthChange,
    }));
    const handle = container.querySelector('.resizer') as HTMLElement;
    expect(handle, 'resizer handle').toBeDefined();

    pointer('pointerdown', handle, 100);
    pointer('pointermove', document, 160); // +60 → 280
    pointer('pointermove', document, 1000); // +900 → clamped to 500
    pointer('pointerup', document, 1000);

    expect(onAsideWidthChange).toHaveBeenCalledWith(500);
  });

  it('without the props the shell behaves exactly as before (no callback, default width)', () => {
    render(shell());
    expect(aside()?.style.width).toBe('220px');
  });
});
