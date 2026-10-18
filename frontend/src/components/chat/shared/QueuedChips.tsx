/** Queued-message strip — the active chat's dsh inbox (see SYSTEM:
 * chat-message-queue). Each entry is a message sent while the chat was busy;
 * dsh runs them in order, each as its own turn. Click ✕ to drop one — the
 * chip leaves when dsh's splice arrives. Rendered as a "ghost" of the user
 * bubble it will become: same geometry (max-w-[85%] min-w-[200px],
 * right-aligned), one tone lighter yellow. */
// see SYSTEM: chat-composer — queued-message plates (entry: ChatComposer.tsx).
// INVARIANT: no rounded corners (project rule) — square backdrop, never rounded-full.
// Why: project-wide `border-radius: 0` rule (CLAUDE.md).
// INVARIANT: geometry mirrors the user bubble in MessageBubble.tsx, background is one
// tone lighter (--warm-tint vs -strong).
// Why: a queued message is the same message not yet sent — it must read as a pale
// preview of the bubble it becomes, not as a separate chip type.

import { X, Image } from 'lucide-react';
import { IconButton } from '../../ui';
import { useTranslation } from '../../../i18n';
import type { QueuedMessage } from '../../../chat/inbox-queue';

interface Props {
  queued: QueuedMessage[];
  onRemove: (messageId: string) => void;
}

export function QueuedChips({ queued, onRemove }: Props) {
  const { t } = useTranslation();
  if (queued.length === 0) return null;
  return (
    <div className="flex flex-col items-end gap-2">
      {queued.map(m => (
        <div
          key={m.id}
          className="group relative max-w-[85%] min-w-[200px] text-text px-3 py-2 pr-7 text-sm leading-relaxed text-right whitespace-pre-wrap break-words cursor-default"
          // Translucent so the agent's streaming answer stays readable underneath.
          style={{ backgroundColor: 'color-mix(in srgb, var(--warm-tint) 80%, transparent)' }}
          title={t('queuedMessageTitle')}
        >
          {m.text.trim() || (m.images > 0 ? null : t('queuedEmpty'))}
          {m.images > 0 && (
            <span className="inline-flex items-center gap-1 ml-2 text-text-dim align-middle" aria-label={t('queuedImages', { count: m.images })}>
              <Image size={12} />{m.images}
            </span>
          )}
          {/* INVARIANT: the ✕ on a yellow chip hovers warm-strong (theme="note"), never the app-wide gray.
              Why: the operator corrected this gray plate several times — a control on a warm surface takes that surface's hover. */}
          <IconButton
            size="sm"
            theme="note"
            className="absolute top-0 right-0 w-5 h-5"
            onClick={() => onRemove(m.id)}
            aria-label={t('removeQueued')}
          >
            <X size={12} />
          </IconButton>
        </div>
      ))}
    </div>
  );
}
