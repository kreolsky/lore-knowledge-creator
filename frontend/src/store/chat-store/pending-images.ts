/** Pending-image actions shared by the AI-chat and note-chat composers. */
import type { PressedImageMeta } from '../../chat/attach-image';

interface PendingImagesState {
  pendingImages: string[];
  pendingImageMeta: Record<string, PressedImageMeta>;
}

interface PendingImagesActions {
  addPendingImage: (dataUrl: string, meta?: PressedImageMeta) => void;
  removePendingImage: (index: number) => void;
  clearPendingImages: () => void;
}

type PendingImagesSet = (fn: (s: PendingImagesState) => Partial<PendingImagesState>) => void;

export function createPendingImagesSlice(set: PendingImagesSet): PendingImagesActions {
  return {
    addPendingImage(dataUrl, meta) {
      set(s => ({
        pendingImages: [...s.pendingImages, dataUrl],
        pendingImageMeta: meta ? { ...s.pendingImageMeta, [dataUrl]: meta } : s.pendingImageMeta,
      }));
    },
    removePendingImage(index) {
      set(s => {
        const removed = s.pendingImages[index];
        const pendingImages = s.pendingImages.filter((_, i) => i !== index);
        // WHY: the same data URL can be attached twice; its meta stays while a copy remains.
        if (removed === undefined || pendingImages.includes(removed)) return { pendingImages };
        const { [removed]: _dropped, ...pendingImageMeta } = s.pendingImageMeta;
        return { pendingImages, pendingImageMeta };
      });
    },
    clearPendingImages() {
      set(() => ({ pendingImages: [], pendingImageMeta: {} }));
    },
  };
}
