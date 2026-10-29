/**
 * The ONE POST /references/upload call — a single FormData contract for both
 * real callers: the ReferencesPanel upload (useReferenceUpload) and the
 * editor's image file drop (useEditorFileDrop). Title is always the file name.
 */
import type { Reference } from '../types';
import { apiClient } from './client';

export function uploadReferenceFile(projectId: string, documentId: string, file: File): Promise<Reference> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('project_id', projectId);
  formData.append('document_id', documentId);
  formData.append('title', file.name);
  return apiClient.upload('/references/upload', formData) as Promise<Reference>;
}
