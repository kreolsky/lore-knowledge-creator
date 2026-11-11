/**
 * Builds the preview gallery of a document: every rendered image in document
 * order, one transclusion level deep, table cells included (row by row, at the
 * table anchor's position). Read from state, never from the DOM —
 * CM6 virtualises off-screen lines, so `.cm-image-embed` lists only what is drawn.
 */

import { EditorState } from '@codemirror/state';
import { ensureSyntaxTree, syntaxTree } from '@codemirror/language';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { markdownExtensions } from '../../editor/markdown-config';
import type * as Y from 'yjs';
import { transcludeMap } from './live-preview/effects';
import { extractEmbedFromImageNode, parseTarget, resolveTransclusionTarget } from './live-preview/transclusion-grammar';
import { readTableModel } from './live-preview/table-block-model';

const PARSE_BUDGET_MS = 1000;

export interface GalleryItem {
  src: string;
  /** Host-document position: the embed's own, or its transclusion's for a nested image. */
  pos: number;
}

/** Calls `visit` with each embed target of `state`, in document order. */
function walkEmbeds(state: EditorState, visit: (urlText: string, from: number) => void): void {
  const tree = ensureSyntaxTree(state, state.doc.length, PARSE_BUDGET_MS) ?? syntaxTree(state);
  tree.iterate({
    enter: (node) => {
      if (node.name !== 'Image') return;
      const embed = extractEmbedFromImageNode(node.node, state);
      if (embed) visit(embed.urlText, node.from);
    },
  });
}

function imageUrlOf(target: { kind: string; id: string }): string | undefined {
  return target.kind === 'image' ? transcludeMap.get(target.id)?.imageUrl : undefined;
}

function markdownState(content: string): EditorState {
  return EditorState.create({
    doc: content,
    extensions: markdown({ base: markdownLanguage, extensions: markdownExtensions }),
  });
}

/** Images of `state` and of its transclusions (one level), all reported at `posOf(from)`. */
function collectLevel(state: EditorState, items: GalleryItem[], posOf: (from: number) => number, ydoc: Y.Doc | null): void {
  walkEmbeds(state, (urlText, from) => {
    const pos = posOf(from);
    const parsed = parseTarget(urlText);
    if (parsed?.scheme === 'table') {
      // WHY: a table's cells live in the `tables` Yjs subtree, not in the text — the
      // anchor is all the buffer holds, so the gallery reads the model directly.
      const model = ydoc ? readTableModel(ydoc, parsed.id) : null;
      model?.rows.flat().forEach((cell) => collectLevel(markdownState(cell), items, () => pos, null));
      return;
    }
    const target = resolveTransclusionTarget(urlText);
    if (!target) return;
    const src = imageUrlOf(target);
    if (src) { items.push({ src, pos }); return; }
    const content = target.kind === 'image' ? undefined : transcludeMap.get(target.id)?.content;
    if (content === undefined) return;
    // WHY no recursion: the nested view degrades deeper embeds to plain links, so
    // only images rendered at this level exist on screen.
    walkEmbeds(markdownState(content), (nestedUrl) => {
      const nested = resolveTransclusionTarget(nestedUrl);
      const nestedSrc = nested ? imageUrlOf(nested) : undefined;
      if (nestedSrc) items.push({ src: nestedSrc, pos });
    });
  });
}

/** All images of the host document: its own, its transclusions' and its tables' cells. */
export function collectGallery(host: EditorState, ydoc: Y.Doc | null): GalleryItem[] {
  const items: GalleryItem[] = [];
  collectLevel(host, items, (from) => from, ydoc);
  return items;
}

/** The clicked occurrence: first match of `src` at or after `pos`, else any match, else -1. */
export function galleryIndexOf(items: GalleryItem[], src: string, pos: number | null): number {
  const after = pos === null ? -1 : items.findIndex((i) => i.src === src && i.pos >= pos);
  return after >= 0 ? after : items.findIndex((i) => i.src === src);
}
