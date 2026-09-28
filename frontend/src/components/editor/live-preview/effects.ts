/**
 * Effects, module-level maps, and utilities shared across live-preview modules.
 *
 * No internal live-preview dependencies — only CM6 core imports.
 */
import { syntaxTree } from '@codemirror/language';
import { ViewPlugin, ViewUpdate } from '@codemirror/view';
import { StateEffect, Facet } from '@codemirror/state';

// SYSTEM: transclusion — nesting-depth of the current EditorView.
// INVARIANT: transclusion renders at most one nesting level — the main editor is
// depth 0; a nested transclusion view is depth 1, and at depth >= 1 an embed is
// rendered as a plain link (build-structural), never another band.  Why: capping transclusion at one level prevents recursive embeds and matches the editor render; deeper embeds show as plain links.
// Why: a transcluded doc can embed itself or form a cycle; this facet caps recursion.
export const transclusionDepth = Facet.define<number, number>({
  combine: (values) => (values.length ? values[values.length - 1] : 0),
});

// INVARIANT: reveal-on-cursor (showing raw markdown under the cursor) is an editing  Why: reveal-on-cursor is for editing; a read-only nested view defaults to position 0, so revealing there would show the first element raw.
// affordance. A read-only nested transclusion view (selection defaults to position 0)
// must NOT reveal — else the first element (heading, bold, table, math, mermaid) shows
// raw. Why: provide this once so every decoration builder shares it and future builders
// inherit it; the depth→behaviour mapping lives in exactly one place (editorExperience).
export const revealAtCursor = Facet.define<boolean, boolean>({
  combine: (v) => (v.length ? v[v.length - 1] : true),
});

/**
 * Module-level bridge map from raw entity id → transclusion entry.
 *
 * ARCH: Intentionally module-level — CM6 extensions (StateField, ViewPlugin)
 * cannot access React Context. This is the standard bridge pattern for
 * React → CM6 data flow. See DECORATION-LAYERS.md for the full architecture.
 *
 * Single owner of ALL transclusion entries: image-ref (kind:'ref-image', carries
 * `imageUrl`), text-ref (kind:'ref-text', carries `content`), and doc
 * (kind:'doc', carries `content`). Image-ref entries live here (NOT in a separate
 * image map), so there is exactly one resolution path for every embed kind.
 */

// see SYSTEM: transclusion — inline embedding of another document/reference content.
// ARCH: Module-level bridge — CM6 plugins can't read React context. Key = raw entity id
// (reference_id or document_id). Populated by useEditorReferenceSync; consumed by
// build-structural (resolveTransclusion) and the TransclusionWidget.
export type TransclusionKind = 'doc' | 'ref-text' | 'ref-image' | 'ref-file';

/**
 * Who seeded an entry — drives the per-source ownership contract in useEditorReferenceSync:
 * a rebuild writer (ancestor/project/doc) may delete+recreate every entry of its OWN
 * source; a content-mutating writer (lazy-fetch, content_flushed) UPDATES an existing
 * entry in place, preserving its original source. Keeps a freshly-reloaded doc entry
 * from being wiped back to loading by the next storeDocuments rebuild.
 */
export type TransclusionSource = 'ancestor-ref' | 'project-ref' | 'doc';

export interface TransclusionEntry {
  kind: TransclusionKind;
  /**
   * Who seeded an entry — drives per-source ownership. OPTIONAL only because legacy
   * entry literals (tests, widget constructors) predate it; EVERY production writer in
   * useEditorReferenceSync sets it. The ownership checks treat `undefined` as "not
   * owned by a rebuild writer" (preserved), which is harmless for non-hook seedings.
   */
  source?: TransclusionSource;
  title: string;
  /** Rendered markdown body (ref-text / doc). Undefined for ref-image/ref-file. */
  content?: string;
  /** Resolved image URL (ref-image only). */
  imageUrl?: string;
  /** Download URL for the stored binary (ref-file only — authed or public-aware). */
  fileUrl?: string;
  /** Stored byte size (ref-file only, from file_meta.file_size). */
  fileSize?: number;
}

export const transcludeMap = new Map<string, TransclusionEntry>();

/** Clears all transclusion entries (image + text-ref + doc). Called on project switch. */
export function clearTranscludeMap(): void {
  transcludeMap.clear();
}

/**
 * Dispatched once after the transclude map and/or link-validity sets change so
 * livePreviewField rebuilds structural decorations. Coalesces the previous
 * `refMapChanged` + `linkDataChanged` pair — see useEditorReferenceSync.
 */
export const linkContextChanged = StateEffect.define<null>();

/**
 * Returns true if >= `threshold` fraction of `next` visible ranges are
 * already covered by `prev` ranges. Used by ViewPlugins to skip expensive
 * decoration rebuilds when the viewport barely moved.
 */
export function isViewportCovered(
  prev: readonly { from: number; to: number }[],
  next: readonly { from: number; to: number }[],
  threshold = 0.8,
): boolean {
  let total = 0;
  let covered = 0;
  for (const nr of next) {
    total += nr.to - nr.from;
    for (const pr of prev) {
      const s = Math.max(nr.from, pr.from);
      const e = Math.min(nr.to, pr.to);
      if (e > s) covered += e - s;
    }
  }
  return total > 0 && covered / total >= threshold;
}

/**
 * Scroll anchor for parser-only tree updates.
 * ARCH: Lezer parser runs async and produces new syntax trees while the user is
 * idle. Each tree update triggers full decoration rebuild in livePreviewField /
 * tableRenderField, which can add/remove Decoration.replace above the viewport
 * (e.g., hiding HeaderMark on a newly parsed heading). CM6 then corrects
 * scrollTop to compensate for height changes, causing a visible micro-jump.
 * This plugin captures the scroll position before the DOM reflow and restores
 * it, eliminating the jump for tree-only (no doc/selection change) updates.
 */
export const scrollAnchorPlugin = ViewPlugin.fromClass(class {
  update(update: ViewUpdate) {
    if (update.docChanged || update.selectionSet) return;
    if (syntaxTree(update.state) === syntaxTree(update.startState)) return;
    const snapshot = update.view.scrollSnapshot();
    requestAnimationFrame(() => {
      try { update.view.dispatch({ effects: snapshot }); } catch {}
    });
  }
});
