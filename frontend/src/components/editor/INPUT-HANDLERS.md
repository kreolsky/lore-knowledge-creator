# Input Handlers & Prec.high Stack

CM6 input interception order is critical — wrong registration order causes handlers to
shadow each other. This document maps all custom high-priority handlers and their ordering
constraints.

## Handler Map

| # | File | Facet | Trigger | Must Precede | Registration |
|---|------|-------|---------|-------------|-------------|
| 1 | `auto-fence-selection.ts` | `inputHandler` (Prec.high) | `` ` `` + non-empty selection | #2 auto-surround | `editorSetup` line 35 |
| 2 | `auto-surround.ts` | `inputHandler` (Prec.high) | `(` `[` `{` `"` `'` + non-empty selection | `closeBrackets()` | `editorSetup` line 36 |
| 3 | `auto-fence.ts` | `inputHandler` (Prec.high) | `` ` `` (3rd backtick at line start) | `closeBrackets()` | `editorSetup` line 37 |
| 4 | `smart-quotes.ts` | `inputHandler` (Prec.high) | `"` `'` `«` `"` `"` (word-boundary pairing) | `closeBrackets()` | `editorSetup` line 38 |
| 5 | `list-continuation.ts` | `keymap` (Prec.high) | Enter, Backspace | default markdown keymap | `cmExtensions` |
| 6 | `hotkey-keymap.ts` | `keymap` (Prec.high) | Mod-* combos (B/I/K/…) | `basicSetup` defaults | `cmExtensions` |
| 7 | `voice-widget.ts` | `keymap` (Prec.high) | Escape (conditional: widget active) | default Escape | `voiceWidgetExtension` |

## Registration Order in `editorSetup`

```ts
autoFenceSelection,   // #1 — handles ` + selection → progressive wrapping
autoSurround,         // #2 — handles bracket/quote + selection
autoFence,            // #3 — handles ``` (third backtick at line start)
smartQuotes,          // #4 — context-aware pairing for " ' « " " (NOT backtick)
closeBrackets(),      // CM6 built-in — must come AFTER custom inputHandlers
```

All three custom inputHandlers use `Prec.high` which elevates them above `closeBruckets()`.
Within `Prec.high`, order in the extension array determines priority: earlier = higher.

### Critical Ordering: #1 before #2

`auto-fence-selection` must be registered before `auto-surround`. Both handle `` ` `` when
there is a selection. If `auto-surround` ran first, it would wrap the selection with single
backticks on every `` ` `` press, preventing progressive wrapping (`` → ``` → fenced block).

`auto-surround.ts` does NOT include `` ` `` in its `PAIRS` map — backtick is intentionally
absent specifically to avoid this conflict. This design decision makes the ordering a safety
net rather than a hard dependency, but the order should still be preserved.

## Known Conflicts

### Backtick ambiguity

Backtick (`` ` ``) has dual behavior:
- **With selection:** `auto-fence-selection` handles progressive wrapping
- **Without selection, 3rd at line start:** `auto-fence` creates fenced code block
- **Without selection, otherwise:** `closeBrackets()` auto-closes inline code

The `PAIRS` map in `auto-surround.ts` intentionally excludes backtick to avoid intercepting
it. `smart-quotes.ts` also excludes backtick for the same reason — inserting a pair would
place the cursor between two backticks, preventing `auto-fence` from ever seeing the
required ``` `` ``` prefix. If backtick is ever added to either PAIRS map, fenced code
block creation via keyboard will break.

## Adding a New Prec.high Handler

**Rule:** Every new `Prec.high` `inputHandler` or `keymap` extension MUST be documented
in this file before merge. Specify:
1. What keys/events it intercepts
2. What it must precede (and why)
3. Where it's registered in the extension array
4. Any known conflicts with existing handlers
