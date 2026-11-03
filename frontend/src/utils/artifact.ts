/**
 * The artifact kind predicate (ONE owner for the stored-value comparison —
 * mirrors backend/artifacts.py `is_artifact_row`). The persisted value stays
 * `media_type === 'html'`; in code the kind is "artifact", never "page"
 * (SYSTEM: page-lock owns "page" for ANY document).
 */
import type { ExportFormat } from '../hooks/useDocumentExport';

/** Whether a doc row is an html artifact (a missing media_type = plain doc). */
export function isArtifactDoc(
  doc: { media_type?: string | null } | null | undefined,
): boolean {
  return (doc?.media_type ?? '') === 'html';
}

/** An artifact downloads only as its own html source — the markdown export
 *  formats would run html through the markdown pipeline and mangle it. */
export const ARTIFACT_EXPORT_FORMATS: ExportFormat[] = ['html'];
