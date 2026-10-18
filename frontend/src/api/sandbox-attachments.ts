/** Chat file attachments: upload into the caller's sandbox workspace and take
 * one back. XHR, not fetch — the composer chip shows upload progress. */
import type { ChatFile } from '../types';
import { apiClient } from './client';

/** A refused or failed upload; `status` 0 = the request never got an answer. */
export class AttachmentUploadError extends Error {
  status: number;
  constructor(status: number) {
    super(`attachment upload failed (${status})`);
    this.status = status;
  }
}

export interface AttachmentUpload {
  promise: Promise<ChatFile>;
  abort: () => void;
}

export function uploadChatAttachment(
  projectId: string, file: File, onProgress: (fraction: number) => void,
): AttachmentUpload {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<ChatFile>((resolve, reject) => {
    xhr.open('POST', `/api/projects/${projectId}/sandbox/attachments`);
    xhr.withCredentials = true;
    xhr.responseType = 'json';
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as ChatFile);
      else reject(new AttachmentUploadError(xhr.status));
    };
    xhr.onerror = () => reject(new AttachmentUploadError(0));
    xhr.onabort = () => reject(new AttachmentUploadError(0));
    const form = new FormData();
    form.append('file', file);
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}

export function deleteChatAttachment(projectId: string, uploadId: string): Promise<unknown> {
  return apiClient.delete(`/projects/${projectId}/sandbox/attachments/${uploadId}`);
}
