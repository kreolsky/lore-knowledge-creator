/** Attach one chat image: press it on the backend, queue the pressed version.
 * The one attach path of the AI and note composers (paste and drop). */
// ARCH: the press runs server-side (POST /chat/images/normalize, SYSTEM:
// chat-image-press) — one codec for chat and references, the client stays thin.
import { apiClient, AuthError, RequestTooLargeError, HttpError } from '../api/client';
import { t } from '../i18n';
import { useAppStore } from '../store/app-store';
import { attachmentBudgetExceeded, dataUrlBytes, formatAttachmentMb, totalAttachmentBytes } from '../utils/attachment-size';

export interface PressedImageMeta {
  width: number;
  height: number;
  sourceWidth: number;
  sourceHeight: number;
}

/** The store side of an attach: one per composer, exported by its store. */
export interface AttachSurface {
  getPending: () => string[];
  add: (dataUrl: string, meta: PressedImageMeta) => void;
  /** +1 when a press starts, -1 when it ends — the composers hold send meanwhile. */
  pressing: (delta: 1 | -1) => void;
}

function pressFailure(err: unknown): string {
  if (err instanceof RequestTooLargeError) return t('chatImageTooLarge');
  if (err instanceof HttpError && err.status === 422) return t('chatImageUnreadable');
  return t('chatImagePressFailed');
}

/** A failed press toasts and adds no chip; the budget counts PRESSED bytes. */
export async function attachImage(file: File, surface: AttachSurface): Promise<void> {
  const { showToast, maxAttachmentMb } = useAppStore.getState();
  surface.pressing(1);
  try {
    const form = new FormData();
    form.append('file', file);
    const reply = await apiClient.upload('/chat/images/normalize', form);
    const pending = totalAttachmentBytes(surface.getPending());
    const bytes = dataUrlBytes(reply.data_url);
    if (attachmentBudgetExceeded(pending, bytes, maxAttachmentMb)) {
      showToast(t('attachmentsExceedLimit', { used: formatAttachmentMb(pending + bytes), limit: maxAttachmentMb }), 'error');
      return;
    }
    surface.add(reply.data_url, {
      width: reply.width,
      height: reply.height,
      sourceWidth: reply.source_width,
      sourceHeight: reply.source_height,
    });
  } catch (err) {
    if (!(err instanceof AuthError)) showToast(pressFailure(err), 'error');
  } finally {
    surface.pressing(-1);
  }
}

/** The size the model gets for an image of w×h: the area cap of the backend's
 * press_bytes (thumbnails.py), no upscale. */
export function pressedSize(w: number, h: number, maxPixels: number): { width: number; height: number } {
  if (w * h <= maxPixels) return { width: w, height: h };
  const scale = Math.sqrt(maxPixels / (w * h));
  return { width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)) };
}
