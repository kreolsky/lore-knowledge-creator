/** Shared pending-image thumbnail strip. Click a thumbnail to remove it.
 * Replaces the duplicated preview markup in both AI and notes composers. */
// SYSTEM: chat-composer — shared composer attachment chips
// INVARIANT: no rounded corners anywhere (project rule) — the hover overlay uses
// a square backdrop, not rounded-full. Why: the AI composer historically used
// rounded-full here; unifying on the note style keeps the whole app square.

import { X } from 'lucide-react';
import { useTranslation } from '../../../i18n';

interface Props {
  images: string[];
  onRemove: (index: number) => void;
}

export function AttachmentChips({ images, onRemove }: Props) {
  const { t } = useTranslation();
  if (images.length === 0) return null;
  return (
    <div className="flex gap-2 mb-2 flex-wrap">
      {images.map((img, i) => (
        <div
          key={i}
          className="group relative w-16 h-16 border border-border cursor-pointer"
          onClick={() => onRemove(i)}
          title={t('removeImage')}
        >
          <img src={img} alt="" className="w-full h-full object-cover" />
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
            <div className="w-10 h-10 bg-black/50 flex items-center justify-center">
              <X size={20} className="text-white" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
