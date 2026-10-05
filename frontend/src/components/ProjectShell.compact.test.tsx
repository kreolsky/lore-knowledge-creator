/** ProjectShell compact viewport — center first, tree drawer, full-screen right panel, persisted layout untouched. */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, createRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MemoryRouter } from 'react-router-dom';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const stableT = (k: string) => k;
vi.mock('../i18n', () => ({ useTranslation: () => ({ t: stableT }) }));
vi.mock('./UserControls', () => ({ UserControls: () => null }));

import { ProjectShell, type ProjectShellHandle, type LeftTabEntry, type RightTabEntry } from './ProjectShell';
import { useUIStore, DEFAULT_DOC_STATE } from '../store/ui-store';
import { useAppStore } from '../store/app-store';
import type { Document, Reference } from '../types';

const ORIGINAL_UI = useUIStore.getState();
const ORIGINAL_APP = useAppStore.getState();

const leftTabs: LeftTabEntry[] = [
  { tab: 'docs', icon: 'D', title: 'docs', renderPanel: () => createElement('div', { 'data-testid': 'tree' }) },
  { tab: 'toc', icon: 'T', title: 'toc', renderPanel: () => null },
];
const rightTabs: RightTabEntry[] = [
  { tab: 'chat', icon: 'C', title: 'chat', renderPanel: () => createElement('div', { 'data-testid': 'chat-panel' }) },
  { tab: 'notes', icon: 'N', title: 'notes', renderPanel: () => createElement('div', { 'data-testid': 'notes-panel' }) },
];

let container: HTMLDivElement;
let root: Root;
let setSidebarOpen: ReturnType<typeof vi.fn<(open: boolean) => void>>;
let setRightPanelOpen: ReturnType<typeof vi.fn<(docId: string | null, open: boolean) => void>>;

function mount(ref?: React.Ref<ProjectShellHandle>) {
  const tree = createElement(MemoryRouter, null, createElement(ProjectShell, {
    ref,
    leftTabs, rightTabs,
    renderCenter: () => createElement('div', { 'data-testid': 'center' }),
    renderHeader: () => createElement('header', null, 'hdr'),
    userControlsVariant: 'full',
    rightPanelDocId: 'doc-1',
    rightPanelReady: true,
    effectiveRightTab: useUIStore.getState().documents['doc-1']?.rightPanelTab ?? 'chat',
  }));
  act(() => { root.render(tree); });
}

const byTestId = (id: string) => container.querySelector(`[data-testid="${id}"]`);
const byTitle = (title: string) => container.querySelector(`button[title="${title}"]`) as HTMLButtonElement | null;
const closeIn = (id: string) => byTestId(id)!.querySelector('button[title="close"]');
const drawerOpen = () => byTestId('shell-left')!.className.includes('compact-drawer--open');
const click = (el: Element | null) => act(() => { (el as HTMLElement).click(); });

beforeEach(() => {
  useUIStore.setState(ORIGINAL_UI, true);
  useAppStore.setState(ORIGINAL_APP, true);
  setSidebarOpen = vi.fn<(open: boolean) => void>();
  setRightPanelOpen = vi.fn<(docId: string | null, open: boolean) => void>();
  useUIStore.setState({
    compactLayout: true,
    sidebarOpen: true,
    documents: { 'doc-1': { ...DEFAULT_DOC_STATE, mainEntity: { type: 'document', id: 'doc-1' }, rightPanelOpen: true } },
    setSidebarOpen, setRightPanelOpen,
  });
  useAppStore.setState({ currentDocument: { document_id: 'doc-1' } as Document });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

describe('ProjectShell — compact viewport', () => {
  it('opens on the center only even when the persisted layout has both panels open', () => {
    mount();
    expect(byTestId('center')).not.toBeNull();
    expect(drawerOpen()).toBe(false);
    expect(byTestId('shell-right')).toBeNull();
    expect(byTestId('compact-scrim')).toBeNull();
    expect(container.querySelector('.left-tab-bar')).toBeNull();
    expect(container.querySelector('.resizer')).toBeNull();
  });

  it('tree button opens the drawer; scrim closes it; tree button toggles it', () => {
    mount();
    click(byTitle('compactToggleTree'));
    expect(drawerOpen()).toBe(true);
    click(byTestId('compact-scrim'));
    expect(drawerOpen()).toBe(false);
    click(byTitle('compactToggleTree'));
    click(byTitle('compactToggleTree'));
    expect(drawerOpen()).toBe(false);
  });

  it('right button opens the full-screen panel; close button closes; only one panel is open', () => {
    mount();
    click(byTitle('compactOpenPanel'));
    expect(byTestId('shell-right')).not.toBeNull();
    expect(byTestId('chat-panel')).not.toBeNull();
    click(closeIn('shell-right'));
    expect(byTestId('shell-right')).toBeNull();

    click(byTitle('compactOpenPanel'));
    click(byTitle('compactToggleTree'));
    expect(drawerOpen()).toBe(true);
    expect(byTestId('shell-right')).toBeNull();

    click(byTitle('compactOpenPanel'));
    expect(byTestId('shell-right')).not.toBeNull();
    expect(drawerOpen()).toBe(false);
  });

  it('drawer tab row has a close button that closes the drawer', () => {
    mount();
    click(byTitle('compactToggleTree'));
    click(closeIn('shell-left'));
    expect(drawerOpen()).toBe(false);
  });

  it('closing the full-screen panel kills a live search overlay', () => {
    mount();
    click(byTitle('compactOpenPanel'));
    act(() => { useUIStore.getState().setRightPanelTab('doc-1', 'search'); });
    expect(useUIStore.getState().searchTabDocId).toBe('doc-1');
    click(closeIn('shell-right'));
    expect(useUIStore.getState().searchTabDocId).toBeNull();
  });

  it('opening a document closes the drawer; opening a reference closes the right panel', () => {
    mount();
    click(byTitle('compactToggleTree'));
    act(() => { useAppStore.setState({ currentDocument: { document_id: 'doc-2' } as Document }); });
    expect(drawerOpen()).toBe(false);

    click(byTitle('compactOpenPanel'));
    act(() => { useAppStore.setState({ currentReference: { reference_id: 'ref-1' } as Reference }); });
    expect(byTestId('shell-right')).toBeNull();
  });

  it('never writes the persisted open state', () => {
    mount();
    click(byTitle('compactToggleTree'));
    click(byTestId('compact-scrim'));
    click(byTitle('compactOpenPanel'));
    click(byTitle('notes'));
    click(closeIn('shell-right'));
    expect(setSidebarOpen).not.toHaveBeenCalled();
    expect(setRightPanelOpen).not.toHaveBeenCalled();
  });

  it("imperative openRightPanel('notes') opens the panel on the notes tab", () => {
    const ref = createRef<ProjectShellHandle>();
    mount(ref);
    expect(byTestId('shell-right')).toBeNull();
    act(() => { ref.current!.openRightPanel('notes'); });
    expect(useUIStore.getState().documents['doc-1'].rightPanelTab).toBe('notes');
    mount(ref); // parent re-derives effectiveRightTab from the store
    expect(byTestId('notes-panel')).not.toBeNull();
    expect(setRightPanelOpen).not.toHaveBeenCalled();
  });

  it('desktop keeps the persisted layout (both panels open, tab bar rendered)', () => {
    useUIStore.setState({ compactLayout: false });
    mount();
    expect(container.querySelector('.left-tab-bar')).not.toBeNull();
    expect(byTestId('shell-right')).not.toBeNull();
    expect(byTitle('compactToggleTree')).toBeNull();
  });
});
