/**
 * Ctrl+Alt+[ / Ctrl+Alt+] global hotkey — steps the document text column through
 * width presets, remembered per project in this browser.
 */
// WHY: window keydown, not the CM6 keymap — same reasoning as useSidebarHotkey
// (see SYSTEM: global-hotkey): the column width is an app-level view setting that
// must work whatever has focus.
// WHY: presets are in rem, not px — the column holds the same number of characters
// at any root font size. The first preset is 844px at 16px, the historical width.

import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { t, type TranslationKey } from '../i18n';

const PRESETS: ReadonlyArray<{ width: string; label: TranslationKey }> = [
  { width: '52.75rem', label: 'textWidthNormal' },
  { width: '66rem', label: 'textWidthWide' },
  { width: '80rem', label: 'textWidthWider' },
];
const CSS_VAR = '--content-max-width';
const STEP_BY_CODE: Record<string, number> = { BracketLeft: -1, BracketRight: 1 };

const storageKey = (projectId: string) => `lore.textWidth.${projectId}`;

function readLevel(projectId: string): number {
  try {
    const level = Number(localStorage.getItem(storageKey(projectId)));
    return Number.isInteger(level) && level >= 0 && level < PRESETS.length ? level : 0;
  } catch {
    return 0; // storage blocked: the default width is the correct rendering
  }
}

function applyLevel(level: number): void {
  document.documentElement.style.setProperty(CSS_VAR, PRESETS[level].width);
}

export function useTextWidthHotkey(projectId: string | undefined): void {
  useEffect(() => {
    if (!projectId) return;
    let level = readLevel(projectId);
    applyLevel(level);

    const handleKeyDown = (e: KeyboardEvent) => {
      // e.code, not e.key: the physical bracket keys, so the chord works on the
      // Russian layout (х/ъ) and with Option altering e.key on macOS.
      const step = STEP_BY_CODE[e.code];
      if (!step || !e.ctrlKey || !e.altKey || e.metaKey || e.shiftKey) return;
      // WHY: Windows reports AltGr as Ctrl+Alt; on layouts where AltGr+bracket-key types
      // a character (Swiss German, Hungarian, Czech) the chord must stay typing.
      if (e.getModifierState('AltGraph')) return;
      e.preventDefault();
      const next = level + step;
      if (next < 0 || next >= PRESETS.length) return;
      level = next;
      applyLevel(level);
      try {
        localStorage.setItem(storageKey(projectId), String(level));
      } catch {
        useAppStore.getState().showToast(t('textWidthNotSaved'), 'warning');
      }
      useAppStore.getState().showToast(t(PRESETS[level].label), 'info');
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      // Public share and other surfaces render the default column.
      document.documentElement.style.removeProperty(CSS_VAR);
    };
  }, [projectId]);
}
