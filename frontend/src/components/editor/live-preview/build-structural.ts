/**
 * Build structural decorations (StateField — full tree, no viewport).
 *
 * Iterates full syntax tree to produce Decoration.replace / mark / widget
 * for headers, emphasis, links, images, code blocks, inline math, etc.
 */
import { syntaxTree } from '@codemirror/language';
import { Decoration, DecorationSet, WidgetType } from '@codemirror/view';
import { EditorState, RangeSetBuilder } from '@codemirror/state';
import { findInlineMath, findSingleLineDisplayMath } from '../math-render';
import { transcludeMap, transclusionDepth, revealAtCursor } from './effects';
import { resolveTransclusionTarget, extractEmbedFromImageNode, parseTarget } from './transclusion-grammar';
import { resolveLinkClass } from './link-types';
import { getListItemContentColumn, getListItemContentLines } from './list-indent';

// charWidth-collapse thresholds from incident 2026-05-30 (see the INVARIANT at the
// whitespace-line replace below, and lessons/2026-05-30).
const CHAR_WIDTH_SAMPLE_MAX = 20;
const WHITESPACE_ONLY_RE = /^\s+$/;

// see SYSTEM: transclusion — resolves an image-node URL to either a widget kind or null.
// Delegates to the shared `resolveTransclusionTarget` resolver (transclusion-grammar.ts),
// the single source of truth for target → kind. The reject set (`data:`/`http`/`note:`…)
// lives there once, mirrored by the backend `transclusion_grammar.py`. Image entries
// live in the unified `transcludeMap` (kind:'ref-image', carries imageUrl).
export function resolveTransclusion(urlText: string): {
  kind: 'image' | 'text' | 'doc' | 'file';
  id: string;
} | null {
  return resolveTransclusionTarget(urlText);
}

// Link class validity is delegated to the link-types registry (single source of
// link-type knowledge). Kept as a thin wrapper for the call sites below.
function resolveLinkCls(cls: string, urlText: string): string {
  return resolveLinkClass(cls, urlText);
}

import {
  CheckboxWidget,
  BulletWidget,
  ImageWidget,
  TransclusionWidget,
  TransclusionLinkWidget,
  FileRefWidget,
  CopyButtonWidget,
  InlineMathWidget,
  parseSizeFromAlt,
} from './widgets';

/**
 * Child node names whose ranges are replaced (hidden) when the cursor
 * is NOT within their PARENT node's range.
 */
export const HIDDEN_MARKERS = new Set([
  'HeaderMark',
  'EmphasisMark',
  'StrikethroughMark',
  'CodeMark',
  'QuoteMark',
]);

export interface ReplaceRange {
  from: number;
  to: number;
  widget?: WidgetType;
  // ARCH: when true, the decoration is a CM6 block widget (renders between
  // lines, not inside a .cm-line) — used by standalone transclusion lines to
  // render gaplessly without the editor line-height strut. Mirrors the table
  // block widget in fields.ts.
  block?: boolean;
}

export interface MarkRange {
  from: number;
  to: number;
  cls: string;
  attributes?: Record<string, string>;
}

export function buildStructuralDecorations(state: EditorState): DecorationSet {
  // INVARIANT: transclusion renders at most one nesting level. Why: a transcluded doc
  // can embed itself or form a cycle; at depth >= 1 an embed degrades to a plain link.
  const depth = state.facet(transclusionDepth);
  // INVARIANT: reveal-on-cursor is an editing affordance. A read-only nested transclusion  Why: reveal-on-cursor is for editing; a read-only nested view defaults to position 0, so honoring the cursor would show the first element (heading/bold/table) raw.
  // view (selection defaults to position 0) must NOT reveal — else the first element
  // (heading, bold, table, math, mermaid) shows raw. Driven by revealAtCursor (see
  // effects.ts); `transclusionDepth` above is still read for the embed-degradation guard.
  const selRanges = state.facet(revealAtCursor) ? state.selection.ranges : [];
  const isCursorIn = (from: number, to: number) =>
    selRanges.some((r) => r.from <= to && r.to >= from);

  const replaceRanges: ReplaceRange[] = [];
  const markRanges: MarkRange[] = [];
  const codeRanges: { from: number; to: number }[] = [];
  const tableRanges: { from: number; to: number }[] = [];

  syntaxTree(state).iterate({
    enter(node) {
      if (node.name === 'Table') {
        tableRanges.push({ from: node.from, to: node.to });
      }

      if (node.name === 'ListItem') {
        const lines = getListItemContentLines(node, state);
        for (const { line, leadingSpaces, isFirst } of lines) {
          if (leadingSpaces > 0 && (isFirst || leadingSpaces >= lines.contentCol)) {
            replaceRanges.push({ from: line.from, to: line.from + leadingSpaces });
          }
        }
      }

      if (node.name === 'ListMark') {
        const markText = state.doc.sliceString(node.from, node.to);
        if (markText !== '*' && markText !== '-') return;

        const listItem = node.node.parent;
        if (!listItem || listItem.name !== 'ListItem') return;
        if (listItem.node.getChildren('TaskMarker').length > 0) return;
        // WHY: accept BOTH `-` and `*` task bullets here.
        // Why: while the syntax tree is mid-reparse (typing), a `* [ ] ` line
        // lacks its TaskMarker node briefly; without `*` in this guard the `*`
        // gets swapped for a BulletWidget while `[ ]` lingers as text, so `*`
        // task items flicker where `-` ones don't. The marker must match `- [ ]`.
        // The ` +` (not a single space) tolerates LLM-emitted multi-space forms
        // like `*   [ ]`. The bullet set `[-*+]` matches the backend
        // normalize_list_spacing marker set so a `+ [ ]` task item isn't left
        // with a stray `+` bullet beside the checkbox. Lezer DOES parse those
        // (TaskMarker is a grandchild via the `Task` node, so
        // getChildren('TaskMarker') above is empty) — this guard is what keeps
        // a BulletWidget from being swapped in for them.
        if (/^\s*[-*+] +\[[ xX]\] /.test(state.doc.lineAt(node.from).text)) return;

        let hideTo = node.to;
        const lineEnd = state.doc.lineAt(node.from).to;
        while (hideTo < lineEnd && state.doc.sliceString(hideTo, hideTo + 1) === ' ') {
          hideTo++;
        }

        if (isCursorIn(node.from, node.to)) return;

        replaceRanges.push({
          from: node.from,
          to: hideTo,
          widget: new BulletWidget(),
        });
        return;
      }

      if (node.name === 'TaskMarker') {
        const parent = node.node.parent;
        if (!parent) return;
        const listItem = parent.parent;
        if (!listItem) return;

        const markerText = state.doc.sliceString(node.from, node.to);
        const checked = markerText === '[x]' || markerText === '[X]';

        // The ListMark belongs to the ListItem, NOT to the `Task` node (which is
        // `parent` and whose firstChild is the TaskMarker itself). Anchoring at the
        // ListMark covers the bullet + ANY run of spaces before `[ ]` so a
        // multi-space `*   [ ]` doesn't leave a bare `*` rendered next to the box.
        const listMark = listItem.firstChild;
        const hideFrom =
          listMark?.name === 'ListMark' ? listMark.from : node.from - 2;

        // INVARIANT: Decoration.replace covers ONLY the `- [ ]` markup,
        // never the trailing space. Including the trailing space puts the
        // cursor on the right boundary of an atomic range at line-end and  Why: covering the trailing space would trap the cursor on the right boundary of an atomic range at line-end; excluding it keeps the cursor reachable.
        // causes browser-specific snapping into the widget on Tab/Cmd+]/click.
        // The trailing space (still required by Lezer for TaskMarker
        // recognition) renders as ordinary whitespace and provides the
        // visual gap between the checkbox and following text.
        const hideTo = node.to;
        const trailingSpace =
          state.doc.sliceString(node.to, node.to + 1) === ' ' ? 1 : 0;
        const contentStart = node.to + trailingSpace;

        if (isCursorIn(hideFrom, node.to)) return;

        replaceRanges.push({
          from: hideFrom,
          to: hideTo,
          widget: new CheckboxWidget(checked, node.from),
        });

        if (checked) {
          const contentCol = getListItemContentColumn({ node: listItem, from: listItem.from }, state);
          let taskEnd = listItem.to;
          const nestedLists = [
            ...listItem.getChildren('BulletList'),
            ...listItem.getChildren('OrderedList'),
          ];
          for (const nl of nestedLists) {
            const nlLineStart = state.doc.lineAt(nl.from).from;
            if (nlLineStart < taskEnd) taskEnd = nlLineStart;
          }
          for (let pos = hideTo; pos < taskEnd; ) {
            const ln = state.doc.lineAt(pos);
            let spaces = 0;
            while (spaces < ln.length && state.doc.sliceString(ln.from + spaces, ln.from + spaces + 1) === ' ') {
              spaces++;
            }
            if (pos > hideTo && spaces < contentCol) {
              taskEnd = ln.from;
              break;
            }
            pos = ln.to + 1;
          }
          // Strike-through skips the trailing space so the gap between the
          // checkbox and content remains visually unstyled.
          markRanges.push({ from: contentStart, to: taskEnd, cls: 'cm-task-done' });
        }
        return;
      }

      if (HIDDEN_MARKERS.has(node.name)) {
        const parent = node.node.parent;
        const revealFrom = parent ? parent.from : node.from;
        const revealTo = parent ? parent.to : node.to;
        if (isCursorIn(revealFrom, revealTo)) return;

        let hideTo = node.to;
        if (node.name === 'HeaderMark' || node.name === 'QuoteMark') {
          if (state.doc.sliceString(node.to, node.to + 1) === ' ') {
            hideTo = node.to + 1;
          }
        }
        replaceRanges.push({ from: node.from, to: hideTo });
        return;
      }

      if (node.name === 'Link' || node.name === 'Image') {
        // ARCH: walk children once for BOTH Link and Image. The Image branch passes this
        // already-walked `children` into `extractEmbedFromImageNode` (via prebuiltChildren)
        // so the helper skips its own traversal — single walk per node. The fall-through
        // LinkMark bracket-hiding below (line ~399) ALSO reads `children`, so it MUST stay
        // populated for Image nodes (non-transclusion images like `![a](http://…)` rely on it).
        const children: { name: string; from: number; to: number }[] = [];
        const cursor = node.node.cursor();
        if (cursor.firstChild()) {
          do {
            children.push({ name: cursor.name, from: cursor.from, to: cursor.to });
          } while (cursor.nextSibling());
        }

        const urlNode = children.find((c) => c.name === 'URL');

        if (node.name === 'Link' && !urlNode) {
          if (node.from > 0 && state.doc.sliceString(node.from - 1, node.from) === '[') {
            const afterLink = state.doc.sliceString(node.to);
            const urlMatch = afterLink.match(/^\]\(([^)]+)\)/);
            if (urlMatch) {
              const urlText = urlMatch[1];
              const fullEnd = node.to + 2 + urlText.length + 1;

              let cls: string | null = null;
              if (urlText.startsWith('note:')) { cls = 'cm-note-link'; }
              else if (urlText.startsWith('ref:')) { cls = 'cm-ref-link'; }
              else if (urlText.startsWith('http')) { cls = 'cm-ext-link'; }
              else if (!urlText.startsWith('#') && !urlText.startsWith('mailto:')) { cls = 'cm-doc-link'; }

              if (cls) {
                const finalCls = resolveLinkCls(cls, urlText);
                const attrs: Record<string, string> = {};
                if (cls.startsWith('cm-doc-link')) attrs['data-link-type'] = 'doc';
                else if (cls.startsWith('cm-ref-link')) attrs['data-link-type'] = 'ref';
                else if (cls.startsWith('cm-note-link')) attrs['data-link-type'] = 'note';
                else if (cls.startsWith('cm-ext-link')) attrs['data-link-type'] = 'ext';
                // data-link-id = urlText verbatim. Doc links are authored as bare ids, so
                // they're already bare; ref/note urlText includes the scheme prefix. The
                // consumer (EditorLinkPreview) normalizes via decodeLinkId in utils/in-app-link.ts,
                // which strips any prefix defensively for all types. See that module for the
                // full contract shared with chat (MarkdownContent, Sources).
                if (attrs['data-link-type']) attrs['data-link-id'] = urlText;
                markRanges.push({ from: node.from, to: node.to, cls: finalCls, attributes: attrs });
              } else {
                markRanges.push({ from: node.from, to: node.to, cls: 'cm-link' });
              }

              if (!isCursorIn(node.from - 1, fullEnd)) {
                replaceRanges.push({ from: node.from - 1, to: node.from });
                replaceRanges.push({ from: node.to, to: fullEnd });
              }
            }
          }
          return false;
        }

        if (urlNode && node.name === 'Link') {
          const urlText = state.doc.sliceString(urlNode.from, urlNode.to);
          const linkMarksAll = children.filter((c) => c.name === 'LinkMark');
          if (linkMarksAll.length >= 2) {
            const labelFrom = linkMarksAll[0].to;
            const labelTo = linkMarksAll[1].from;
            if (labelTo > labelFrom) {
              let cls: string | null = null;
              if (urlText.startsWith('note:')) {
                cls = 'cm-note-link';
              } else if (urlText.startsWith('ref:')) {
                cls = 'cm-ref-link';
              } else if (urlText.startsWith('http')) {
                cls = 'cm-ext-link';
              } else if (!urlText.startsWith('#') && !urlText.startsWith('mailto:')) {
                cls = 'cm-doc-link';
              }
              if (cls) {
                const finalCls = resolveLinkCls(cls, urlText);
                const attrs: Record<string, string> = {};
                if (cls.startsWith('cm-doc-link')) attrs['data-link-type'] = 'doc';
                else if (cls.startsWith('cm-ref-link')) attrs['data-link-type'] = 'ref';
                else if (cls.startsWith('cm-note-link')) attrs['data-link-type'] = 'note';
                else if (cls.startsWith('cm-ext-link')) attrs['data-link-type'] = 'ext';
                // data-link-id = urlText verbatim. Doc links are authored as bare ids, so
                // they're already bare; ref/note urlText includes the scheme prefix. The
                // consumer (EditorLinkPreview) normalizes via decodeLinkId in utils/in-app-link.ts,
                // which strips any prefix defensively for all types. See that module for the
                // full contract shared with chat (MarkdownContent, Sources).
                if (attrs['data-link-type']) attrs['data-link-id'] = urlText;
                markRanges.push({ from: labelFrom, to: labelTo, cls: finalCls, attributes: attrs });
              } else {
                markRanges.push({ from: labelFrom, to: labelTo, cls: 'cm-link' });
              }
            }
          }
        }

        const hasOuterBrackets = node.name === 'Link'
          && node.from > 0
          && state.doc.sliceString(node.from - 1, node.from) === '['
          && node.to < state.doc.length
          && state.doc.sliceString(node.to, node.to + 1) === ']';

        const revealFrom = hasOuterBrackets ? node.from - 1 : node.from;
        const revealTo = hasOuterBrackets ? node.to + 1 : node.to;
        if (isCursorIn(revealFrom, revealTo)) return false;

        if (node.name === 'Image') {
          const embed = extractEmbedFromImageNode(node.node, state, children);
          if (embed) {
            const { urlText, alt } = embed;
            // `table:` anchors are owned by `tableBlockField` (block replace) — skip ALL
            // inline processing here so a revealed anchor (cursor on the line) shows its
            // raw `![label](table:id)` text instead of half-hidden link brackets.
            if (parseTarget(urlText)?.scheme === 'table') return false;
            const resolved = resolveTransclusion(urlText);
            if (resolved) {
              if (resolved.kind === 'image') {
                // ImageWidget path — ref:<id> image references. The URL now lives in the
                // unified transcludeMap (kind:'ref-image', carries imageUrl).
                const { width, height } = parseSizeFromAlt(alt);
                const imgUrl = transcludeMap.get(resolved.id)?.imageUrl;
                replaceRanges.push({ from: node.from, to: node.to, widget: new ImageWidget(imgUrl!, width, height) });
                return false;
              }
              if (resolved.kind === 'file') {
                // see SYSTEM: transclusion — a file ref (agent-shared archive) has no
                // text body: the embed is a compact download card, never a band.
                const entry = transcludeMap.get(resolved.id)!;
                replaceRanges.push({ from: node.from, to: node.to, widget: new FileRefWidget(entry, resolved.id) });
                return false;
              }
              // see SYSTEM: transclusion — content embed (ref-text or doc).
              const entry = transcludeMap.get(resolved.id)!;
              const kind = resolved.kind === 'doc' ? 'doc' : 'ref';
              // Depth guard: inside a nested transclusion view, render the embed as a
              // plain link instead of another band (caps recursion / cycles).
              if (depth >= 1) {
                replaceRanges.push({ from: node.from, to: node.to, widget: new TransclusionLinkWidget(entry, resolved.id, kind) });
                return false;
              }
              const widget = new TransclusionWidget(entry, resolved.id, kind, depth);
              // ARCH: standalone lines (only the embed + surrounding whitespace on
              // the line) render as a CM6 block widget — gapless, no line-height
              // strut, fills .cm-content width. Mirrors the table block widget
              // (fields.ts). Whole-line range avoids the orphan-line charWidth
              // scroll-jump bug; block-range cursor reveal preserves
              // body-click-to-edit-raw (clicking the band lands the cursor in
              // range → rebuild hides the widget → raw syntax shown).
              const lineFrom = state.doc.lineAt(node.from).from;
              const lineTo = state.doc.lineAt(node.to).to;
              const beforeText = state.doc.sliceString(lineFrom, node.from);
              const afterText = state.doc.sliceString(node.to, lineTo);
              const onlyWs = (s: string) => s.length === 0 || WHITESPACE_ONLY_RE.test(s);
              const isStandalone = onlyWs(beforeText) && onlyWs(afterText);
              if (isStandalone) {
                if (isCursorIn(lineFrom, lineTo)) return false;
                replaceRanges.push({ from: lineFrom, to: lineTo, widget, block: true });
                return false;
              }
              // Inline fallback (mid-paragraph): keep the inline widget.
              replaceRanges.push({ from: node.from, to: node.to, widget });
              return false;
            }
          }
        }

        const linkMarks = children.filter((c) => c.name === 'LinkMark');

        if (node.name === 'Image' && linkMarks.length >= 1) {
          replaceRanges.push({ from: linkMarks[0].from, to: linkMarks[0].to });
        } else if (linkMarks.length >= 1) {
          replaceRanges.push({ from: linkMarks[0].from, to: linkMarks[0].to });
        }

        if (linkMarks.length >= 2) {
          replaceRanges.push({
            from: linkMarks[1].from,
            to: linkMarks[linkMarks.length - 1].to,
          });
        }

        return false;
      }

      if (node.name === 'HorizontalRule') {
        if (isCursorIn(node.from, node.to)) return;
        replaceRanges.push({ from: node.from, to: node.to });
        return false;
      }

      if (node.name === 'InlineCode') {
        codeRanges.push({ from: node.from, to: node.to });
        const contentFrom = node.from + 1;
        const contentTo = node.to - 1;
        if (contentTo > contentFrom) {
          const rawContent = state.doc.sliceString(contentFrom, contentTo);
          const trimmed = rawContent.trim();
          const colorRe = /^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8})(?:\s+(.+))?$/;
          const match = colorRe.exec(trimmed);
          if (match) {
            const color = match[1];
            const label = match[2];

            markRanges.push({
              from: node.from,
              to: node.to,
              cls: 'cm-color-swatch',
              attributes: { style: `background-color:${color}` },
            });

            if (!isCursorIn(node.from, node.to)) {
              const leadingSpaces = rawContent.length - rawContent.trimStart().length;
              const colorStart = contentFrom + leadingSpaces;
              if (label !== undefined) {
                const prefixMatch = /^#[0-9a-fA-F]{3,8}\s+/.exec(trimmed);
                const prefixLen = prefixMatch ? prefixMatch[0].length : color.length + 1;
                replaceRanges.push({ from: colorStart, to: colorStart + prefixLen });
              } else {
                replaceRanges.push({ from: colorStart, to: colorStart + color.length });
              }
            }
          }
        }
        return;
      }

      if (node.name === 'FencedCode') {
        codeRanges.push({ from: node.from, to: node.to });
        const cursorInside = isCursorIn(node.from, node.to);

        const cursor = node.node.cursor();
        let openLine: { from: number; to: number } | null = null;
        let closeLine: { from: number; to: number } | null = null;
        let codeText = '';
        let language = '';

        if (cursor.firstChild()) {
          do {
            if (cursor.name === 'CodeMark') {
              const ln = state.doc.lineAt(cursor.from);
              if (!openLine) openLine = { from: ln.from, to: ln.to };
              else closeLine = { from: ln.from, to: ln.to };
            } else if (cursor.name === 'CodeInfo') {
              language = state.doc.sliceString(cursor.from, cursor.to).trim();
            } else if (cursor.name === 'CodeText') {
              codeText = state.doc.sliceString(cursor.from, cursor.to);
            }
          } while (cursor.nextSibling());
        }

        // WHY: when the cursor is outside the block, hide the raw fence  Why: with the cursor outside a fenced block the raw ```/```lang marker lines are hidden (opening → copy button, closing → nothing) so only rendered content shows.
        // marker lines (``` / ```lang) — replace the opening line with the copy
        // button (language shown as its label) and the closing line with nothing.
        // Why: a short (<=20-char) pure-ASCII marker line rendered in the CODE
        // font is a valid charWidth sample for CM6's measureTextSize(); its
        // monospace width swings the height-oracle estimate and re-triggers the
        // 2026-05-30 scroll-jump. Hiding the raw markers removes the sampleable
        // code-font line. See lessons/2026-05-30.
        // WHY: with the cursor inside, the fence lines stay raw for editing, but the
        // copy button is still offered — as a zero-width widget at the end of the
        // opening line (absolutely positioned) — so copying never requires moving
        // the cursor out of the block first.
        if (cursorInside) {
          if (openLine && codeText) {
            replaceRanges.push({ from: openLine.to, to: openLine.to, widget: new CopyButtonWidget(codeText, language) });
          }
          return false;
        }
        if (openLine) {
          replaceRanges.push({
            from: openLine.from,
            to: openLine.to,
            widget: codeText ? new CopyButtonWidget(codeText, language) : undefined,
          });
        }
        if (closeLine && closeLine.from !== openLine?.from) {
          replaceRanges.push({ from: closeLine.from, to: closeLine.to });
        }

        return false;
      }
    },
  });

  const inCode = (from: number, to: number) =>
    codeRanges.some((r) => from < r.to && to > r.from);
  const inTable = (from: number, to: number) =>
    tableRanges.some((r) => from >= r.from && to <= r.to);

  for (let i = 1; i <= state.doc.lines; i++) {
    const line = state.doc.line(i);

    // INVARIANT: a line that is ONLY whitespace (and short) must not render its
    // raw spaces — hide them with a replace.
    // Why: CM6's measureTextSize() (HeightOracle.charWidth) samples any rendered
    // line that is <= 20 chars and pure ASCII; a spaces-only line makes charWidth
    // collapse to the width of a space (~4px vs ~7px real). That halves the wrap
    // estimate for every OFF-SCREEN line, so contentHeight drops far below the
    // real DOM and CM6 clamps scrollTop — the multi-thousand-px scroll jump.
    // Hiding the whitespace removes the plain-text child, so CM6 skips the line
    // and falls back to its stable Latin sample. See incident 2026-05-30.
    if (line.length > 0 && line.length <= CHAR_WIDTH_SAMPLE_MAX && WHITESPACE_ONLY_RE.test(line.text)
        && !inCode(line.from, line.to) && !inTable(line.from, line.to)
        && !isCursorIn(line.from, line.to)) {
      replaceRanges.push({ from: line.from, to: line.to });
    }

    const displayMatches = findSingleLineDisplayMath(line.text, line.from);
    const inDisplay = (from: number, to: number) =>
      displayMatches.some((d) => from >= d.from && to <= d.to);

    const inlineMatches = findInlineMath(line.text, line.from);
    for (const m of inlineMatches) {
      if (inDisplay(m.from, m.to)) continue;
      if (inCode(m.from, m.to)) continue;
      if (inTable(m.from, m.to)) continue;
      if (isCursorIn(m.from, m.to)) continue;
      replaceRanges.push({ from: m.from, to: m.to, widget: new InlineMathWidget(m.latex) });
    }
  }

  // ARCH: Merge adjacent non-widget replace ranges to reduce decoration count.
  // RISK: this merge only coalesces replace ranges — it does NOT detect overlaps
  // between replaceRanges and markRanges. If a mark range and a replace range cover
  // the same position, RangeSetBuilder will throw at runtime. See DECORATION-LAYERS.md.
  replaceRanges.sort((a, b) => a.from - b.from || a.to - b.to);

  const mergedReplace: ReplaceRange[] = [];
  for (const r of replaceRanges) {
    const last = mergedReplace[mergedReplace.length - 1];
    if (last && !last.widget && !r.widget && r.from < last.to) {
      last.to = Math.max(last.to, r.to);
    } else {
      mergedReplace.push({ ...r });
    }
  }

  const allDecs: { from: number; to: number; dec: Decoration }[] = [];

  for (const r of mergedReplace) {
    allDecs.push({
      from: r.from,
      to: r.to,
      dec: r.widget
        ? r.block
          ? Decoration.replace({ widget: r.widget, block: true })
          : Decoration.replace({ widget: r.widget })
        : Decoration.replace({}),
    });
  }

  for (const m of markRanges) {
    allDecs.push({
      from: m.from,
      to: m.to,
      dec: Decoration.mark({ class: m.cls, ...(m.attributes && { attributes: m.attributes }) }),
    });
  }

  allDecs.sort((a, b) => a.from - b.from || a.to - b.to);

  const builder = new RangeSetBuilder<Decoration>();
  for (const r of allDecs) {
    builder.add(r.from, r.to, r.dec);
  }

  return builder.finish();
}
