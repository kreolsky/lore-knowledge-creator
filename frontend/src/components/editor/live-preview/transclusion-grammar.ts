/**
 * Shared transclusion grammar — single source of truth for resolving a raw embed
 * `target` string (`![alt](target)`) to its transclusion `kind`.
 *
 * Two layers:
 *  - `parseTarget` — PURE parser (no map access): canonical scheme detection
 *    (`ref:` / `doc:` / bare-doc). Returns null for every NON-transclusion target.
 *  - `resolveTransclusionTarget` — wraps parseTarget + looks up the unified
 *    `transcludeMap`, returning the concrete render `kind` (image / text / doc).
 *
 * ARCH: the markdown parsing of the `Image` node itself (Lezer tree traversal) is
 * handled by each consumer via `extractEmbedFromImageNode` (below); only the
 * target-string semantics live here, so the two never drift.
 *
 * INVARIANT: the reject set MUST be identical on the backend
 * (`transclusion_grammar.py`). Why: both sides must agree on what is
 * NOT a transclusion, or a bare `data:`/`http` target resolves inconsistently between
 * the editor preview and the export pipeline. Plan 1.3 made the drift structurally
 * impossible: `NON_TRANSCLUSION_RE` + `TransclusionScheme` below are CODE-GENERATED
 * from the canonical `SCHEME_TABLE` (`backend/transclusion_grammar.py`) by
 * `.claude/scripts/transclusion-grammar-codegen.py` (CI-gated `--check`) — there is no
 * hand-written copy left to drift.
 */

import type { EditorState } from '@codemirror/state';
import type { SyntaxNode } from '@lezer/common';
import { NON_TRANSCLUSION_RE, type TransclusionScheme } from './transclusion-grammar.generated';
import { transcludeMap, type TransclusionEntry } from './effects';

/**
 * Pure parser. Given a raw target string, decide its scheme + id.
 * Returns null for every non-transclusion target (URL, mailto, #, note:, data:).
 *
 * INVARIANT: `table:` is a DOC-LOCAL target — the id is a key in the HOST document's own
 * `tables` Y.Map, NOT a shared entity in `transcludeMap`. Why: a table belongs to one
 * document. It is parsed here (both sides agree it IS a transclusion) but resolved
 * locally by the table widget dispatch in build-structural, never via
 * `resolveTransclusionTarget`/`transcludeMap`. Mirror: `transclusion_grammar.py`.
 */
export function parseTarget(rawTarget: string): { scheme: TransclusionScheme; id: string } | null {
  if (rawTarget.startsWith('ref:')) {
    return { scheme: 'ref', id: rawTarget.slice(4) };
  }
  if (rawTarget.startsWith('doc:')) {
    return { scheme: 'doc', id: rawTarget.slice(4) };
  }
  if (rawTarget.startsWith('table:')) {
    return { scheme: 'table', id: rawTarget.slice(6) };
  }
  if (NON_TRANSCLUSION_RE.test(rawTarget)) return null;
  // Bare id — a document transclusion (doc links are authored as bare ids).
  return { scheme: 'bare-doc', id: rawTarget };
}

/**
 * Full resolver. Wraps `parseTarget` and consults the unified `transcludeMap` to
 * decide the concrete render `kind`:
 *  - `ref:` + ref-image entry  → image
 *  - `ref:` + ref-file entry   → file (a compact download-card widget)
 *  - `ref:` + ref-text entry   → text
 *  - `doc:` / bare-doc entry   → doc
 * Returns null when the target is non-transclusion OR no entry is present.
 */
export function resolveTransclusionTarget(rawTarget: string): { kind: 'image' | 'text' | 'doc' | 'file'; id: string } | null {
  const parsed = parseTarget(rawTarget);
  if (!parsed) return null;
  // `table:` is doc-local — resolved by the table-block widget dispatch, never via
  // transcludeMap (INVARIANT in parseTarget). It is not an image/text/doc kind.
  if (parsed.scheme === 'table') return null;
  if (parsed.scheme === 'ref') {
    const entry: TransclusionEntry | undefined = transcludeMap.get(parsed.id);
    if (!entry) return null;
    if (entry.kind === 'ref-image') return { kind: 'image', id: parsed.id };
    if (entry.kind === 'ref-file') return { kind: 'file', id: parsed.id };
    return { kind: 'text', id: parsed.id };
  }
  // doc: / bare-doc
  if (transcludeMap.has(parsed.id)) return { kind: 'doc', id: parsed.id };
  return null;
}

export interface EmbedNodeExtract {
  /** The raw URL text (e.g. `ref:abc`, `doc:x`, or a bare id). */
  urlText: string;
  /** The alt text between `![` and `]`. */
  alt: string;
  /**
   * Absolute document coordinates of the Image node (`{ from, to }` in `state.doc`
   * space). `RangeSetBuilder` requires sorted absolute ranges (no-overlap invariant).
   */
  range: { from: number; to: number };
}

/**
 * Lezer-decoupled helper (Task 3): given a syntax node that is an `Image`, walk its
 * children once to extract the URL + alt text + absolute range. Returns null for a
 * non-Image node OR an Image with no URL child.
 *
 * Contract: `range` is in ABSOLUTE doc coordinates — Lezer child positions read via the
 * SyntaxNode cursor are absolute, matching the inline decoration range the
 * `RangeSetBuilder` consumes (no-overlap invariant preserved).
 *
 * `prebuiltChildren` (optional): if the caller has ALREADY walked this node's children
 * (e.g. `build-structural` walks them for its LinkMark bracket-hiding), pass them in to
 * avoid a redundant second traversal. When omitted, the helper walks them itself.
 */
export function extractEmbedFromImageNode(
  node: SyntaxNode,
  state: EditorState,
  prebuiltChildren?: { name: string; from: number; to: number }[],
): EmbedNodeExtract | null {
  if (node.name !== 'Image') return null;

  const children = prebuiltChildren ?? (() => {
    const arr: { name: string; from: number; to: number }[] = [];
    const cursor = node.cursor();
    if (cursor.firstChild()) {
      do {
        arr.push({ name: cursor.name, from: cursor.from, to: cursor.to });
      } while (cursor.nextSibling());
    }
    return arr;
  })();

  const urlNode = children.find((c) => c.name === 'URL');
  if (!urlNode) return null;

  const urlText = state.doc.sliceString(urlNode.from, urlNode.to);
  const altFrom = node.from + 2;
  const closeBracket = children.find((c) => c.name === 'LinkMark' && c.from > node.from + 1);
  const altTo = closeBracket ? closeBracket.from : altFrom;
  const alt = state.doc.sliceString(altFrom, altTo);

  return { urlText, alt, range: { from: node.from, to: node.to } };
}
