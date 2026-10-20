/**
 * Builds a CodeMirror keymap Extension from a HotkeyConfig.
 *
 * Uses Prec.high to override default keybindings from basicSetup
 * (e.g. Mod-i=selectParentSyntax in @codemirror/commands).
 */
// ARCH: Prec.high — must override CM6 default keybindings.

import { EditorView, keymap, runScopeHandlers, ViewPlugin, type KeyBinding } from '@codemirror/view';
import { type Extension, Prec } from '@codemirror/state';
import { takesNotesWithoutFocus } from '../../editor/note-surface';
import { DEFAULT_HOTKEYS, type HotkeyConfig, type MarkdownAction } from './hotkey-config';
import { markdownActionRegistry } from './markdown-actions';

// Actions that need no edit right: their bindings also join the `note` scope, which
// readonlyNoteHotkeys runs for a view the user cannot type into.
const NO_EDIT_ACTIONS: ReadonlySet<MarkdownAction> = new Set(['createNote']);
const NOTE_SCOPE = 'note';

export function buildKeymapExtension(config: HotkeyConfig = DEFAULT_HOTKEYS): Extension {
  const bindings: KeyBinding[] = [];

  for (const [key, action] of Object.entries(config)) {
    const handler = markdownActionRegistry[action];
    if (!handler) continue;
    const scope = NO_EDIT_ACTIONS.has(action) ? `editor ${NOTE_SCOPE}` : undefined;
    bindings.push({ key, run: handler, preventDefault: true, scope });
  }

  return Prec.high(keymap.of(bindings));
}

/**
 * Run the `note`-scope bindings (Mod-m) for a NON-editable view holding the page's text
 * selection.
 *
 * WHY: a non-editable view is not focusable, so its keymap never sees a keydown — the
 * key lands on <body>. Only the `note` scope runs from here: the editor scope also
 * holds Backspace/Enter/formatting commands, which dispatch regardless of `editable`,
 * so a focusable readonly view would write text the server then drops.
 */
export const readonlyNoteHotkeys = ViewPlugin.fromClass(class {
  private readonly view: EditorView;
  private readonly onKeyDown: (event: KeyboardEvent) => void;

  constructor(view: EditorView) {
    this.view = view;
    this.onKeyDown = (event) => this.handle(event);
    window.addEventListener('keydown', this.onKeyDown);
  }

  private handle(event: KeyboardEvent): void {
    const { view } = this;
    if (!takesNotesWithoutFocus(view.state) || event.defaultPrevented) return;
    const anchor = document.getSelection()?.anchorNode;
    if (!anchor || !view.contentDOM.contains(anchor)) return;
    runScopeHandlers(view, event, NOTE_SCOPE);
  }

  destroy(): void {
    window.removeEventListener('keydown', this.onKeyDown);
  }
});
