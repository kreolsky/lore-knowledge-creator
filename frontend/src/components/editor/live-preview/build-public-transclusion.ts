/**
 * Pure seed of the module-level `transcludeMap` for the public-share surface.
 *
 * SYSTEM: transclusion (public-viewer variant) — the /s/:token Editor cannot
 * mount `useEditorReferenceSync` (its project-ref fetch + /documents/batch are
 * authed → 401 on anonymous callers). This builder is the public-path seed:
 * it reads already-loaded public data (store `documents` + `references`) and
 * returns the next-state Map. The `usePublicTransclusionSync` hook owns the
 * commit (clear + set + dispatch `linkContextChanged`).
 *
 * ARCH: pure + unit-testable — returns a fresh Map, does NOT touch the
 * module-level `transcludeMap`, which is co-owned by the authed
 * `useEditorReferenceSync` (per-source ownership contract). Keeping
 * the builder separate lets the seed shape be asserted without mounting the
 * editor.
 *
 * Source tagging mirrors the authed contract:
 *   - ref entries (image + text) → source: 'ancestor-ref'
 *   - doc entries → source: 'doc'
 * (Never 'project-ref' — the public surface has no project-wide ref fetch.)
 *
 * Doc entries seed LOADING (content undefined) for siblings; the open doc
 * (carries content) seeds resolved. The lazy effect in `usePublicTransclusionSync`
 * fills loading entries via `publicDocumentByDoc(id)` — best-effort, leaves
 * them loading on 404/failure (an out-of-scope `doc:` target 404s and must NOT
 * error the page).
 */

import type { Document, Reference } from '../../../types';
import type { TransclusionEntry } from './effects';
import { referenceFileUrl } from '../../../utils/reference-url';

/**
 * Build the next-state transclude map for the public viewer.
 *
 * @param documents  store `documents` (open doc carries content; siblings don't)
 * @param references store `references` (public_references returns rows via
 *                   `SELECT *`, so ref bodies are already present — no lazy
 *                   ref fetch needed; only embedded sibling-doc bodies are
 *                   fetched lazily by the hook)
 * @returns a fresh Map keyed by entity id; the caller owns the commit
 */
export function buildPublicTransclusionMap(
  documents: Document[],
  references: Reference[],
): Map<string, TransclusionEntry> {
  const map = new Map<string, TransclusionEntry>();

  for (const ref of references) {
    if (ref.media_type === 'image') {
      // WHY require file_path: the image URL is built from it. An image ref
      // without a file_path is a broken upload — skip rather than seed a
      // broken-image widget.
      if (!ref.file_path) continue;
      map.set(ref.reference_id, {
        kind: 'ref-image',
        source: 'ancestor-ref',
        title: ref.title,
        imageUrl: referenceFileUrl(ref.reference_id, ref.file_path),
      });
      continue;
    }
    if (ref.media_type === 'file') {
      // File ref (agent-shared archive): a download card, not a band. Same
      // file_path guard as the image branch (public-aware URL builder).
      if (!ref.file_path) continue;
      map.set(ref.reference_id, {
        kind: 'ref-file',
        source: 'ancestor-ref',
        title: ref.title,
        fileUrl: referenceFileUrl(ref.reference_id, ref.file_path),
        fileSize: ref.file_meta?.file_size,
      });
      continue;
    }
    // Text/markdown ref — only seed if it actually has body content. The
    // public_references response carries `content` via SELECT *, so empty
    // refs are genuinely empty (not "not yet loaded") and have nothing to
    // embed — skip them so the band doesn't render a blank loading state.
    const content = ref.content;
    if (content === undefined || !content.trim()) continue;
    map.set(ref.reference_id, {
      kind: 'ref-text',
      source: 'ancestor-ref',
      title: ref.title,
      content,
    });
  }

  for (const doc of documents) {
    // The open doc carries content (PublicSharePage merges it in on doc
    // select); siblings only have tree metadata. Seed LOADING for siblings —
    // the hook lazily fetches bodies for docs the editor text actually embeds.
    const next: TransclusionEntry = doc.content && doc.content.trim()
      ? { kind: 'doc', source: 'doc', title: doc.title, content: doc.content }
      : { kind: 'doc', source: 'doc', title: doc.title };
    map.set(doc.document_id, next);
  }

  return map;
}
