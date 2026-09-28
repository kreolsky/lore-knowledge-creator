/**
 * Unified content batch — ONE POST /api/documents/batch resolves N transcluded
 * doc/reference ids instead of N per-id GETs.
 *
 * ARCH: the batch endpoint accepts both regular documents and references
 * (documents.is_reference=true) under one projected SELECT, so a single call
 * backs both transclusion branches. The per-item projection reuses buildRefPreview
 * (useReferencePreview.ts) — the SAME rule the hover-preview path uses — so a
 * reference rendered from the batch and one rendered from a hover can never
 * diverge. The caller (transclusion lazy-fetch effect) seeds the per-id preview
 * caches from this map so a later hover-preview is a free hit.
 */
import { apiClient, HttpError } from './client';
import { buildRefPreview } from '../hooks/useReferencePreview';
import type { RefPreview } from '../hooks/useReferencePreview';

interface BatchItem {
  document_id: string;
  content?: string;
  media_type?: string;
  file_path?: string;
  is_reference?: boolean;
}

/**
 * Fetch content/image previews for a set of document + reference ids in one
 * batch POST. Returns a map keyed by id. Empty/missing/revoked ids are simply
 * absent from the result (the server omits them rather than 404-ing the batch).
 *
 * An all-missing batch 404s (uniform single-doc posture); that is mapped to an
 * empty map here so callers leave those ids loading instead of failing the whole
 * transclusion set. Non-404 errors propagate.
 */
export async function fetchContentBatch(ids: string[]): Promise<Record<string, RefPreview>> {
  if (ids.length === 0) return {};
  try {
    const { items } = await apiClient.post('/documents/batch', { ids }) as { items: BatchItem[] };
    const out: Record<string, RefPreview> = {};
    for (const it of items ?? []) {
      // document_id is the entity's OWN id (a reference is a document with
      // is_reference=true), which is what transclusionMap / ref:<id> keys on.
      out[it.document_id] = buildRefPreview({
        reference_id: it.document_id,
        media_type: it.media_type,
        file_path: it.file_path,
        content: it.content,
      } as never);
    }
    return out;
  } catch (err) {
    if (err instanceof HttpError && err.status === 404) return {};
    throw err;
  }
}
