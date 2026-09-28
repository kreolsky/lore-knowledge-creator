/**
 * Reference file URL builder.
 *
 * Single source of truth for the backend `/api/files/{reference_id}/{basename}`
 * URL used by every reference image/file preview (hover popups, editor link
 * previews, content pickers, the lazy preview cache). Centralizing the basename
 * extraction keeps the file-serving contract from drifting across call sites.
 *
 * PUBLIC SHARE: when the public page mounts, it calls `setPublicFileContext(rootDocId)`
 * with the published subtree's ROOT document id, so subsequent calls build the
 * anonymous document-keyed surface (`/api/public/documents/{rootDocId}/files/…`)
 * instead of the authed one. The authed surface 401s anonymous callers; the
 * public surface is the single funnel for ref binaries
 * (INVARIANT backend/routes/public_share.py: ref's owning doc ∈ doc_ids). The
 * root resolves the share for the whole subtree (resolve_share walks ancestors),
 * so one stable id covers every file in it. Cleared on unmount.
 */

// Null = authed surface (default). Non-null = anonymous public-share surface,
// holding the published subtree's ROOT document_id (NOT a token — plan
// "public-document-ids" re-keyed the funnel on the document uuid).
let _publicDocId: string | null = null;

export function setPublicFileContext(rootDocumentId: string | null): void {
  _publicDocId = rootDocumentId;
}

export function getPublicFileContext(): string | null {
  return _publicDocId;
}

export function referenceFileUrl(referenceId: string, filePath: string): string {
  const basename = filePath.split('/').pop();
  if (_publicDocId) {
    return `/api/public/documents/${_publicDocId}/files/${referenceId}/${basename}`;
  }
  return `/api/files/${referenceId}/${basename}`;
}

/**
 * Thumb URL builder — the public-aware counterpart to `referenceFileUrl`.
 *
 * WHY this takes NO path arg (asymmetry vs `referenceFileUrl(id, filePath)`):
 * the thumb route is a literal `/thumb` segment (no basename) on both the
 * authed (`/api/files/{id}/thumb`) and public
 * (`/api/public/documents/{rootDocId}/files/{id}/thumb`) surfaces. The basename
 * in `referenceFileUrl` exists only because the file route captures `{filename}`
 * and serves the original — the thumb handler ignores any "filename" entirely.
 * Do NOT "normalize" the two signatures; the path arg here would always be a
 * placeholder.
 */
export function referenceThumbUrl(referenceId: string): string {
  if (_publicDocId) {
    return `/api/public/documents/${_publicDocId}/files/${referenceId}/thumb`;
  }
  return `/api/files/${referenceId}/thumb`;
}
