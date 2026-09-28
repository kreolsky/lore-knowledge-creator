/** Hotkey→action mapping for CM6 editor. Mod = Cmd on Mac, Ctrl on others. */
// SYSTEM: hotkeys — declarative key chord → action mapping for CM6 editor

export type MarkdownAction =
  | 'bold' | 'italic' | 'strikethrough'
  | 'inlineCode' | 'codeBlock'
  | 'indent' | 'outdent'
  | 'clearFormatting'
  | 'heading1' | 'heading2' | 'heading3' | 'heading4'
  | 'quote'
  | 'checkboxList' | 'bulletList' | 'numberedList'
  | 'createLink' | 'createNote'
  | 'voiceInput'
  | 'highlightColor'
  | 'insertTable'
  | 'openFind'
  | 'workWithSelection';

export type HotkeyConfig = Record<string, MarkdownAction>;

export const DEFAULT_HOTKEYS: HotkeyConfig = {
  'Mod-b':       'bold',
  'Mod-i':       'italic',
  'Shift-Mod-x': 'strikethrough',
  'Mod-e':       'inlineCode',
  'Mod-[':       'outdent',
  'Mod-]':       'indent',
  'Tab':         'indent',
  'Shift-Tab':   'outdent',
  'Alt-Mod-e':   'codeBlock',
  'Alt-Mod-0':   'clearFormatting',
  'Alt-Mod-1':   'heading1',
  'Alt-Mod-2':   'heading2',
  'Alt-Mod-3':   'heading3',
  'Alt-Mod-4':   'heading4',
  'Mod-k':       'createLink',
  'Mod-m':       'createNote',
  'Mod-d':       'voiceInput',
  'Shift-Mod-9': 'checkboxList',
  'Shift-Mod-0': 'bulletList',
  'Shift-Mod-8': 'numberedList',
  'Mod-o':       'highlightColor',
  'Shift-Mod-t': 'insertTable',
  'Mod-f':       'openFind',
  'Mod-j':       'workWithSelection',
};
