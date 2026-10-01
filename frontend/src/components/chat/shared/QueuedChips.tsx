/** Shared queued-message strip.
 * Each entry is a follow-up the user typed while a turn was streaming. Click ✕ to drop
 * one. Rendered as a "ghost" of the user bubble it will become: same geometry
 * (max-w-[85%] min-w-[200px], right-aligned), one tone lighter yellow. */
// see SYSTEM: chat-composer — queued-message plates (entry: ChatComposer.tsx).
// INVARIANT: no rounded corners (project rule) — square backdrop, never rounded-full.
// Why: project-wide `border-radius: 0` rule (CLAUDE.md).
// INVARIANT: geometry mirrors the user bubble in MessageBubble.tsx, background is one
// tone lighter (--sticky-yellow-light vs -dark).
// Why: a queued message is the same message not yet sent — it must read as a pale
// preview of the bubble it becomes, not as a separate chip type.

import { X } from 'lucide-react';
import { IconButton } from '../../ui';
import { useTranslation } from '../../../i18n';

interface Props {
  queued: string[];
  onRemove: (index: number) => void;
}

export function QueuedChips({ queued, onRemove }: Props) {
  const { t } = useTranslation();
  if (queued.length === 0) return null;
  return (
    <div className="flex flex-col items-end gap-2">
      {queued.map((text, i) => (
        <div
          key={i}
          className="group relative max-w-[85%] min-w-[200px] text-text px-3 py-2 pr-7 text-sm leading-relaxed text-right whitespace-pre-wrap break-words cursor-default"
          // Translucent so the agent's streaming answer stays readable underneath.
          style={{ backgroundColor: 'color-mix(in srgb, var(--sticky-yellow-light) 80%, transparent)' }}
          title={t('queuedMessageTitle')}
        >
          {text.trim() || t('queuedEmpty')}
          <IconButton
            size="sm"
            className="absolute top-0 right-0 w-5 h-5"
            onClick={() => onRemove(i)}
            aria-label={t('removeQueued')}
          >
            <X size={12} />
          </IconButton>
        </div>
      ))}
    </div>
  );
}
