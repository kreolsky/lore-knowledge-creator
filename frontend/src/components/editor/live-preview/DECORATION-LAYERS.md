# Decoration Layers ADR

Live-preview is composed of four CM6 extensions that form a **layered partition** over the
syntax tree. Each layer owns a disjoint set of node types, preventing range conflicts.

## Extension Table

| # | Extension | Type | File | Target Nodes | Decoration Types |
|---|-----------|------|------|-------------|-----------------|
| 1 | `livePreviewField` | StateField | `build-structural.ts` | HeaderMark, EmphasisMark, StrikethroughMark, CodeMark, QuoteMark, HorizontalRule, ListMark, TaskMarker, InlineCode, Link, Image, InlineMath, FencedCode (copy button) | replace, mark, widget |
| 2 | `livePreviewPlugin` | ViewPlugin | `build-line.ts` | Blockquote, HorizontalRule (line), FencedCode (line bg), ListItem (indent) | line |
| 3 | `tableRenderField` | StateField | `fields.ts` | Table | replace (block) |
| 4 | `mathBlockRenderField` | StateField | `fields.ts` | BlockMath ($$...$$) | replace (block) |
| 5 | `tableBlockField` | StateField | `fields.ts` | Image (`table:` target) | replace (block) |
| 6 | `atomicSelectedField` | StateField | `atomic-delete-guard.ts` | mark overlay on fully-selected ranges of 1 (image embeds) and 5 | mark |
| 7 | `dropCaretField` (via `dropCaret()`) | StateField | `../drop-caret.ts` | none — position-driven | widget (point) |

`tableBlockField` owns `![label](table:id)` anchors exclusively — `build-structural.ts`
early-returns on a `table:` Image so the two never decorate the same range.

## Atomic blocks (image embeds + table anchors)

One rule for both, «мы не удаляем таблицу/картинку одним символом — только выделив её
целиком» (never deleted by a single character — only selected whole):

- **No raw reveal.** An image embed resolving to an image renders its `ImageWidget`
  under EVERY caret position (`build-structural.ts` skips the reveal check for it);
  table anchors never revealed either (layer 5 is cursor-insensitive). Raw source is
  editable only in plain-text mode (Mod-/ detaches the whole render group).
- **Atomic motion.** Both register `EditorView.atomicRanges` (layer 1 via
  `inlineAtomicRanges` for Checkbox+Image widgets; layer 5 via `blockReplaceField`
  `atomic: true`) — arrows jump over the block, Shift+Arrow selects it whole.
- **Guarded deletes.** `atomicDeleteGuard` (a transaction filter in
  `atomic-delete-guard.ts`, mounted inside `renderExtensions()`) drops any `delete.*`
  user event whose change overlaps a guarded range or line-joins into/out of a
  table anchor line while the start selection is empty. Selection deletes, cut over
  a real selection, and remote Yjs transactions pass. Checkboxes are NOT guarded —
  Backspace still removes them whole.

## Composition Order

Extensions are registered in `Editor.tsx` (lines 149-152) and `NoteContentView.tsx` (lines 19-22):

```ts
livePreviewField,     // 1 — structural (full tree)
livePreviewPlugin,    // 2 — line decorations (viewport only)
tableRenderField,     // 3 — table block replace
mathBlockRenderField, // 4 — math block replace
```

Order between extensions 3-4 is interchangeable — they target non-overlapping node types.
Extension 1 must precede extension 2 because `livePreviewField` hides leading spaces
(ListItem ranges) that `livePreviewPlugin` then positions with CSS hanging indent.

## Ownership Rule

**One range — one owner.** Every syntax node type is decorated by exactly one extension.
Before adding a new node type decoration, verify:

1. No other layer already decorates the same node type.
2. If replacing text, the range does not overlap with another layer's replace ranges.

### Exceptions

- `CopyButtonWidget`: created in `livePreviewField` (build-structural.ts) as a widget
  decoration, but the code block's line-level background and fence styling come from
  `livePreviewPlugin` (build-line.ts). This cross-layer collaboration is safe because
  widget decorations and line decorations occupy different CM6 decoration slots.

- `InlineMath`: handled in `build-structural.ts` only if not inside a FencedCode, Table,
  or display math range. Block math ($$...$$) is owned by `mathBlockRenderField`.

- `dropCaretField` (layer 7): NON-SYNTAX decoration — a point widget at an
  arbitrary position (the pointer-drag drop point, driven imperatively by
  `setEditorDropCaret` from refDragAdapter; mounted in `renderExtensions()` and
  editable cell views). It owns no syntax range, so the partition rule does not
  apply; it never overlaps another layer's ranges as a claim, only as a visual
  caret.

- `atomicSelectedField` (layer 6): a mark overlay whose ranges are OWNED by layers
  1 and 5 — the same-range-different-slot collaboration as `CopyButtonWidget` (mark
  vs replace occupy different decoration slots, so no RangeSetBuilder conflict).
  `atomic-delete-guard.ts` documents the shared atomic-block contract.

## Adding a New Node Type

1. **Identify the layer:** inline syntax → `livePreviewField`, line styling →
   `livePreviewPlugin`, multi-line block replace → new/existing StateField.
2. **Check for conflicts:** search all four layers for the node type name.
3. **Test cursor interaction:** when cursor is inside the node's range, decorations
   must be suppressed (reveal raw markdown) — use `isCursorIn(from, to)`. Exceptions:
   atomic blocks (image embeds, table anchors — see *Atomic blocks* above) never
   reveal; their raw text is reachable only through plain-text mode (Mod-/).
4. **Update this document.**

## Internal Risk: mark/replace Overlap

Within `livePreviewField` (`build-structural.ts`), `markRanges` and `replaceRanges`
are accumulated separately and merged in a single pass. If a mark range and a replace
range overlap the same document region, CM6's `RangeSetBuilder` will throw at runtime.

The merge logic (build-structural.ts lines 351-361) sorts and merges adjacent
non-widget replace ranges, but does NOT check for mark/replace overlaps. When adding
new decorations, ensure:
- Replace ranges and mark ranges never cover the same `from..to`.
- When both are needed (e.g., hide link syntax + style link text), the replace range
  should cover the syntax markers and the mark range should cover only the visible label.
