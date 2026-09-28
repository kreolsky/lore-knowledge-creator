/** Content picker anchor resolution — pure helper deciding which document the
 * chat "add context" picker is anchored to for proximity sorting.
 *
 * ARCH: The picker ranks references and documents by proximity to what the
 * user is currently looking at (the OPEN document). Session-based resolution
 * (ref owning doc → session doc → sessionRefId owning doc) serves only as a
 * fallback when no document is open (project-level chat). This mirrors the
 * anchor pattern used by useSortedSessions — the open document is always the
 * primary signal. */

export function resolveContentPickerAnchor(
  currentDocId: string | null,
  activeSession: { reference_id?: string | null; document_id?: string | null } | null | undefined,
  sessionRefId: string | null | undefined,
  references: Pick<{ reference_id: string; document_id: string | null }, 'reference_id' | 'document_id'>[],
): string | null {
  if (currentDocId) return currentDocId;
  if (activeSession?.reference_id) {
    return references.find(r => r.reference_id === activeSession.reference_id)?.document_id
      ?? activeSession.document_id ?? null;
  }
  if (activeSession?.document_id) return activeSession.document_id;
  if (sessionRefId) {
    return references.find(r => r.reference_id === sessionRefId)?.document_id ?? null;
  }
  return null;
}
