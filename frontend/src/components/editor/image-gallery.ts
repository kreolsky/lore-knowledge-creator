/**
 * Builds the preview gallery of a document: every rendered image in document
 * order, one transclusion level deep. Read from state, never from the DOM —
 * CM6 virtualises off-screen lines, so `.cm-image-embed` lists only what is drawn.
 */

import { EditorState } from '@codemirror/state';
import { ensureSyntaxTree, syntaxTree } from '@codemirror/language';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { markdownExtensions } from '../../editor/markdown-config';
import { transcludeMap } from './live-preview/effects';
import { extractEmbedFromImageNode, resolveTransclusionTarget } from './live-preview/transclusion-grammar';

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

/** All images of the host document, including those of its transclusions. */
export function collectGallery(host: EditorState): GalleryItem[] {
  const items: GalleryItem[] = [];
  walkEmbeds(host, (urlText, pos) => {
    const target = resolveTransclusionTarget(urlText);
    if (!target) return;
    const src = imageUrlOf(target);
    if (src) { items.push({ src, pos }); return; }
    const content = target.kind === 'image' ? undefined : transcludeMap.get(target.id)?.content;
    if (content === undefined) return;
    const body = EditorState.create({
      doc: content,
      extensions: markdown({ base: markdownLanguage, extensions: markdownExtensions }),
    });
    // WHY no recursion: the nested view degrades deeper embeds to plain links, so
    // only images rendered at this level exist on screen.
    walkEmbeds(body, (nestedUrl) => {
      const nested = resolveTransclusionTarget(nestedUrl);
      const nestedSrc = nested ? imageUrlOf(nested) : undefined;
      if (nestedSrc) items.push({ src: nestedSrc, pos });
    });
  });
  return items;
}

/** The clicked occurrence: first match of `src` at or after `pos`, else any match, else -1. */
export function galleryIndexOf(items: GalleryItem[], src: string, pos: number | null): number {
  const after = pos === null ? -1 : items.findIndex((i) => i.src === src && i.pos >= pos);
  return after >= 0 ? after : items.findIndex((i) => i.src === src);
}
