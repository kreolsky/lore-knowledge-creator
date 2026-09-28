/** TDD for useSidebarHotkey — Cmd/Ctrl+\ toggles the left sidebar ONLY.
 *
 * Contract under test (see useSidebarHotkey.ts):
 *   - Cmd+\ / Ctrl+\ flips `sidebarOpen`.
 *   - The right panel (per-document `rightPanelOpen`) is NEVER touched.
 *   - Plain '\' without a modifier is ignored (no toggle, no preventDefault).
 *
 * Persistence is inert here: no app bridge is registered, so setSidebarOpen's
 * saveUIStateNow → triggerSaveUI early-returns (see ui-store.ts triggerSaveUI).
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useSidebarHotkey } from './useSidebarHotkey';
import { useUIStore, type DocumentUIState } from '../store/ui-store';

// Per-doc right-panel slices with both values — asserting the hotkey never
// rewrites either direction (it must not close an open right panel, nor open
// a closed one).
function docSlice(rightPanelOpen: boolean): DocumentUIState {
  return { ...useUIStore.getState().getDocState(null), rightPanelOpen };
}

describe('useSidebarHotkey', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    useUIStore.setState({
      sidebarOpen: true,
      documents: {
        'doc-open-right': docSlice(true),
        'doc-closed-right': docSlice(false),
      },
    });
    act(() => {
      root.render(createElement(function Probe() {
        useSidebarHotkey();
        return null;
      }));
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    document.body.innerHTML = '';
    useUIStore.setState({ sidebarOpen: true, documents: {} });
  });

  function fireKey(key: string, mods: Partial<KeyboardEventInit> = {}) {
    const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
    window.dispatchEvent(ev);
    return ev;
  }

  it('Cmd+\\ closes an open sidebar and leaves the right panel untouched', () => {
    fireKey('\\', { metaKey: true });
    expect(useUIStore.getState().sidebarOpen).toBe(false);
    expect(useUIStore.getState().documents['doc-open-right'].rightPanelOpen).toBe(true);
    expect(useUIStore.getState().documents['doc-closed-right'].rightPanelOpen).toBe(false);
  });

  it('Cmd+\\ opens a closed sidebar and leaves the right panel untouched', () => {
    useUIStore.setState({ sidebarOpen: false });
    fireKey('\\', { metaKey: true });
    expect(useUIStore.getState().sidebarOpen).toBe(true);
    expect(useUIStore.getState().documents['doc-open-right'].rightPanelOpen).toBe(true);
    expect(useUIStore.getState().documents['doc-closed-right'].rightPanelOpen).toBe(false);
  });

  it('Ctrl+\\ (non-macOS chord) toggles the sidebar', () => {
    fireKey('\\', { ctrlKey: true });
    expect(useUIStore.getState().sidebarOpen).toBe(false);
    fireKey('\\', { ctrlKey: true });
    expect(useUIStore.getState().sidebarOpen).toBe(true);
  });

  it('plain \\ without a modifier is ignored — no toggle, no preventDefault', () => {
    const ev = fireKey('\\');
    expect(useUIStore.getState().sidebarOpen).toBe(true);
    expect(ev.defaultPrevented).toBe(false);
  });

  it('consumes the chord (preventDefault) so no literal backslash leaks', () => {
    const ev = fireKey('\\', { metaKey: true });
    expect(ev.defaultPrevented).toBe(true);
  });
});
