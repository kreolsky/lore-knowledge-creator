/** Files slice — composer file chips: upload into the sender's sandbox
 * workspace on drop, take back on removal, hand the uploaded ones to the send. */
import type { ChatFile } from '../../types';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import {
  AttachmentUploadError, deleteChatAttachment, uploadChatAttachment,
} from '../../api/sandbox-attachments';
import { CHAT_FILE_MAX_MB } from '../../chat/file-attachment-policy';
import { uuid } from '../../utils/uuid';
import type { ChatState, PendingFile, Set, Get } from './types';
import { registerChatResetHandler } from './reset-registry';

/** The live uploads' aborts, by chip key. */
const uploadAborts = new Map<string, () => void>();
registerChatResetHandler(() => {
  for (const abort of uploadAborts.values()) abort();
  uploadAborts.clear();
});

/** The toast of a refused upload — the server's status names the reason. */
function uploadErrorText(err: unknown, name: string): string {
  const status = err instanceof AttachmentUploadError ? err.status : 0;
  if (status === 413) return t('chatFileTooLarge', { name, limit: CHAT_FILE_MAX_MB });
  if (status === 415) return t('chatFileTypeRefused', { name });
  if (status === 503) return t('chatFileNoSandbox');
  return t('chatFileUploadFailed', { name });
}

function patchFile(set: Set, key: string, patch: Partial<PendingFile>): void {
  set(s => ({ pendingFiles: s.pendingFiles.map(f => (f.key === key ? { ...f, ...patch } : f)) }));
}

/** Take an uploaded file back out of the workspace. A failure is said, not
 * swallowed: the file stays in the sandbox although its chip is gone. */
function takeBack(projectId: string, uploaded: ChatFile): void {
  deleteChatAttachment(projectId, uploaded.id).catch(() => {
    useAppStore.getState().showToast(t('chatFileRemoveFailed', { name: uploaded.name }), 'error');
  });
}

type FilesSlice = Pick<
  ChatState,
  'addPendingFile' | 'removePendingFile' | 'clearPendingFiles' | 'restorePendingFiles'
>;

export function createFilesSlice(set: Set, get: Get): FilesSlice {
  return {
    addPendingFile(projectId: string, file: File) {
      const key = uuid();
      set(s => ({
        pendingFiles: [...s.pendingFiles, {
          key, projectId, name: file.name, size: file.size,
          mime: file.type || 'application/octet-stream', status: 'uploading', progress: 0,
        }],
      }));
      const upload = uploadChatAttachment(projectId, file, progress => patchFile(set, key, { progress }));
      uploadAborts.set(key, upload.abort);
      upload.promise.then(
        (uploaded) => {
          uploadAborts.delete(key);
          // The chip was removed (or the chat reset) while uploading: the file
          // landed anyway, so it goes straight back out.
          if (!get().pendingFiles.some(f => f.key === key)) {
            takeBack(projectId, uploaded);
            return;
          }
          patchFile(set, key, { status: 'done', progress: 1, uploaded, name: uploaded.name });
        },
        (err: unknown) => {
          uploadAborts.delete(key);
          if (!get().pendingFiles.some(f => f.key === key)) return;
          patchFile(set, key, { status: 'error' });
          useAppStore.getState().showToast(uploadErrorText(err, file.name), 'error');
        },
      );
    },

    removePendingFile(key: string) {
      const chip = get().pendingFiles.find(f => f.key === key);
      if (!chip) return;
      set(s => ({ pendingFiles: s.pendingFiles.filter(f => f.key !== key) }));
      uploadAborts.get(key)?.();
      if (chip.uploaded) takeBack(chip.projectId, chip.uploaded);
    },

    clearPendingFiles() {
      set({ pendingFiles: [] });
    },

    restorePendingFiles(projectId: string, files: ChatFile[]) {
      set(s => ({
        pendingFiles: [...s.pendingFiles, ...files.map(uploaded => ({
          key: uuid(), projectId, name: uploaded.name, size: uploaded.size, mime: uploaded.mime,
          status: 'done' as const, progress: 1, uploaded,
        }))],
      }));
    },
  };
}
