/**
 * The ONE POST /references/upload call — a single FormData contract for every
 * caller: the ReferencesPanel upload (useReferenceUpload), the editor's image file
 * drop (useEditorFileDrop), the header REC upload (ProjectPage) and the recording
 * cache re-send (recording-cache). Title is always the file name. apiClient's errors
 * propagate unchanged — callers branch on AuthError / tierRefusal.
 */
import type { Reference } from '../types';
import { apiClient } from './client';

export function uploadReferenceFile(
  projectId: string, documentId: string, file: File, opts?: { idempotencyKey?: string },
): Promise<Reference> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('project_id', projectId);
  formData.append('document_id', documentId);
  formData.append('title', file.name);
  if (opts?.idempotencyKey) formData.append('idempotency_key', opts.idempotencyKey);
  return apiClient.upload('/references/upload', formData) as Promise<Reference>;
}
