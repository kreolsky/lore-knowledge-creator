/** Chat file attachments — the client-side mirror of the upload route's refusals
 * (backend/routes/sandbox.py), so a refused drop toasts at once instead of
 * after uploading. The server stays the authority: it re-checks all three. */

/** Mirrors routes/sandbox.CHAT_ATTACHMENT_MAX_MB. */
export const CHAT_FILE_MAX_MB = 100;

export type FileRefusal = 'size' | 'type';

/** Why a dropped file cannot be attached, or null when it can. */
export function fileRefusal(file: { name: string; type: string; size: number }): FileRefusal | null {
  if (file.name.toLowerCase().endsWith('.rar')) return 'type';
  if (file.size > CHAT_FILE_MAX_MB * 1024 * 1024) return 'size';
  return null;
}
