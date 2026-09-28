/**
 * Builds a CodeMirror keymap Extension from a HotkeyConfig.
 *
 * Uses Prec.high to override default keybindings from basicSetup
 * (e.g. Mod-i=selectParentSyntax in @codemirror/commands).
 */
// ARCH: Prec.high — must override CM6 default keybindings.

import { keymap, type KeyBinding } from '@codemirror/view';
import { type Extension, Prec } from '@codemirror/state';
import { DEFAULT_HOTKEYS, type HotkeyConfig } from './hotkey-config';
import { markdownActionRegistry } from './markdown-actions';

export function buildKeymapExtension(config: HotkeyConfig = DEFAULT_HOTKEYS): Extension {
  const bindings: KeyBinding[] = [];

  for (const [key, action] of Object.entries(config)) {
    const handler = markdownActionRegistry[action];
    if (handler) bindings.push({ key, run: handler, preventDefault: true });
  }

  return Prec.high(keymap.of(bindings));
}
