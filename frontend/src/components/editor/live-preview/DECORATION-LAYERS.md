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

`tableBlockField` owns `![label](table:id)` anchors exclusively — `build-structural.ts`
early-returns on a `table:` Image so the two never decorate the same range.

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

## Adding a New Node Type

1. **Identify the layer:** inline syntax → `livePreviewField`, line styling →
   `livePreviewPlugin`, multi-line block replace → new/existing StateField.
2. **Check for conflicts:** search all four layers for the node type name.
3. **Test cursor interaction:** when cursor is inside the node's range, decorations
   must be suppressed (reveal raw markdown) — use `isCursorIn(from, to)`.
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
