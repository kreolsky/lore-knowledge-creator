# CSS Map — CodeMirror-related styles

Maps every `.cm-*` CSS block to its source file and explains why it lives where it does.

## Editor Layout (index.css lines 435-457)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.editor-cm` | 438 | Ancestor selector for height fill — requires wrapper class |
| `.editor-cm .cm-editor` | 439 | Cross-boundary: parent wrapper → CM6-generated element |
| `.editor-cm .cm-scroller` | 440 | Same — height fill |
| `.editor-content .cm-editor` | 441-444 | Ancestor selector — transparent bg, cursor |
| `.editor-content .cm-editor.cm-focused` | 445-448 | Ancestor + pseudo-class — cannot use EditorView.theme |
| `.editor-content .cm-editor .cm-scroller` | 449-451 | Font override via ancestor |
| `.editor-content .cm-editor .cm-content` | 452-457 | Padding + max-width via ancestor |

**Cannot migrate:** EditorView.theme() scopes to `.ͼXX` (instance-unique class). These rules
need ancestor selectors (`.editor-cm`, `.editor-content`) that exist outside CM6's DOM scope.

## Scrollbar (index.css lines 795-799)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.cm-scroller::-webkit-scrollbar` | 796-799 | `::placeholder` and `::-webkit-scrollbar` pseudo-elements are not supported by `EditorView.theme()` |

## Voice Widget (index.css lines 210-217)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.cm-voice-widget` | 210-217 | Inline alignment overrides for widget DOM. Could move to `EditorView.theme()` but widget is instance-independent DOM. |

**Source:** `editor/voice-widget.ts` → `VoiceRecordingWidget.toDOM()`

## Code Copy Button (index.css lines 801-833)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.cm-code-copy-btn` | 802-824 | Widget DOM styling — positioned absolutely within code fence line |
| `.cm-code-copy-btn--label` | 825-833 | Language label variant |

**Source:** `live-preview/widgets.ts` → `CopyButtonWidget.toDOM()`

## Task Checkbox (index.css lines 835-847)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.cm-task-checkbox` | 836-847 | Widget DOM styling — `<input type="checkbox">` appearance |

**Source:** `live-preview/widgets.ts` → `CheckboxWidget.toDOM()`

## Table Widget (index.css lines 849-879)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.cm-table-widget` | 850-879 | Widget DOM styling — rendered `<table>` element |

**Source:** `live-preview/table-widget.ts` → `TableWidget.toDOM()`

## Math Rendering (index.css lines 881-904)

| Selector | Lines | Why in index.css |
|----------|-------|-----------------|
| `.cm-math-inline` | 882-887 | Inline math widget layout — neutralizes inherited hanging indent |
| `.cm-math-block` | 888-894 | Block math widget layout — same indent fix |
| `.cm-math-block .katex-display` | 895-898 | KaTeX display mode override |
| `.cm-math-error` | 899-904 | Error state styling for failed math rendering |

**Source:** `live-preview/widgets.ts` → `InlineMathWidget`, `BlockMathWidget.toDOM()`

## CM6 Theme (editor/theme.ts)

All scoped styles using `EditorView.theme()` — these live in `theme.ts`:
- Typography, colors, spacing for headings, emphasis, code, links, lists, blockquotes
- Selection highlight overrides
- Fenced code block backgrounds and fence line styling

**Why theme.ts:** `EditorView.theme()` generates instance-scoped CSS with high specificity,
avoiding `!important` and cross-instance leaks. Preferred for all styles that don't need
ancestor selectors or unsupported pseudo-elements.
