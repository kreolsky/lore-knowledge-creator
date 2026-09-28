/**
 * Cmd+\ global hotkey — toggles the left sidebar (docs tree / TOC) open/closed.
 *
 * Mounted in ProjectPage alongside the other global window-level hotkeys
 * (useRecordingHotkey, useEditorHotkeys).
 */
// SYSTEM: global-hotkey — window keydown, not CM6 editor keymap.
//
// ARCH: Deliberately uses window.addEventListener('keydown') instead of the
// declarative hotkey registry in editor/hotkey-config.ts. That registry is a
// CodeMirror 6 keymap Extension (see editor/hotkey-keymap.ts) — it only fires
// while the editor has focus and is bound to markdownActionRegistry (text
// formatting actions). Panel toggling is an app-level action that must work
// regardless of focus: editor focused, cursor in the sidebar, typing in the
// chat input, or no document open at all. All global (non-editor) hotkeys in
// this project follow the same window keydown hook pattern — adding this chord
// to hotkey-config would be the wrong layer.
//
// ARCH: left sidebar ONLY. The right panel (references/chat/notes) is
// per-document state and is deliberately never touched by this chord — its
// open/close stays with tab-bar clicks and the drag-enter reopen path in
// ProjectShell.

import { useEffect } from 'react';
import { useUIStore } from '../store/ui-store';

export function useSidebarHotkey() {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Cmd on macOS, Ctrl elsewhere. The modifier means the chord never
      // inserts a literal '\' into a focused text field, so it's safe to
      // intercept unconditionally.
      if (!(e.metaKey || e.ctrlKey) || e.key !== '\\') return;
      e.preventDefault();

      const ui = useUIStore.getState();
      ui.setSidebarOpen(!ui.sidebarOpen);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);
}
