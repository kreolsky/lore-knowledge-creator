/** Unified chat composer — store-agnostic input area shared by AI and notes chat.
 * Owns the vertical drag-resizer (moved out of ChatPanel) and lays out the
 * textarea + slots. AI-specific chrome (content picker, model/mode dropdowns,
 * warnings, selection pill) arrives via ReactNode slots; notes passes none. */
// ARCH: store-agnostic — no useChatStore / useNoteChatStore calls inside. Each
// chat keeps a thin wrapper that reads its store and forwards props/slots.
// SYSTEM: chat-composer — shared input composer (resize + textarea + slots)

import { type ReactNode, type RefObject } from 'react';
import { Send, Square } from 'lucide-react';
import { Button, FieldTextarea } from '../ui';
import { useVerticalDragResize } from '../../hooks/useVerticalDragResize';
import { useTranslation } from '../../i18n';
import { MicButton } from './shared/MicButton';
import { AttachmentChips } from './shared/AttachmentChips';

interface Props {
  variant: 'ai' | 'note';
  // Experimental: paint the text-input field itself sticky-yellow — used for the
  // AI zero/ghost chat (no active session). Only the textarea, not the panel.
  highlight?: boolean;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  onSend: () => void;
  onStop?: () => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  onPaste?: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  isStreaming: boolean;
  canSend: boolean;
  placeholder: string;
  disabled?: boolean;
  // Mic
  recording: boolean;
  transcribing: boolean;
  onToggleRecording: () => void;
  // Attachments
  images: string[];
  onRemoveImage: (index: number) => void;
  // Plan chat-message-queue: optional follow-up chips typed while a turn streams (AI
  // chat only; notes pass nothing). Empty → not rendered.
  // AI-only slots (omitted by notes)
  topControls?: ReactNode;
  leftControls?: ReactNode;
  warnings?: ReactNode;
  // Focus: wrapper owns the ref so clarify-insert / pendingInputFocus flows work.
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}

export function ChatComposer({
  variant,
  highlight = false,
  value,
  onChange,
  onSend,
  onStop,
  onKeyDown,
  onPaste,
  isStreaming,
  canSend,
  placeholder,
  disabled = false,
  recording,
  transcribing,
  onToggleRecording,
  images,
  onRemoveImage,
  topControls,
  leftControls,
  warnings,
  textareaRef,
}: Props) {
  const { t } = useTranslation();
  const { height: inputHeight, handleRef: dragHandleRef } = useVerticalDragResize({
    defaultHeight: 300,
    // INVARIANT: input min-height equals the default floor (200) — the form must
    // never shrink below this size. The default height (300) is taller than the
    // floor, so the form opens at 300 but can be dragged down to 200.  Why: the min-height (200) is the floor the form can't shrink below; the default (300) opens taller and drags down to 200 but no further.
    minHeight: 200,
    maxHeight: 600,
  });

  // Variant theming: notes use sticky-yellow surfaces; AI uses the neutral surface.
  const panelBg =
    variant === 'note'
      ? 'bg-[var(--sticky-yellow-light)]'
      : 'bg-surface';
  const panelBorderClass =
    variant === 'note'
      ? 'border-[var(--sticky-yellow-sep)]'
      : 'border-border';

  return (
    <>
      <div ref={dragHandleRef} className="resizer-v" />
      <div
        className={`flex-shrink-0 border-t pb-safe ${panelBg} ${panelBorderClass}`}
        style={{ minHeight: inputHeight }}
      >
        <div className="flex flex-col h-full">
          {/* Top area: AI controls + shared attachment chips + warnings */}
          <div className="px-3 pt-3">
            {topControls}
            <AttachmentChips images={images} onRemove={onRemoveImage} />
            {warnings}
          </div>

          {/* Textarea fills the drag-resized height (no auto-grow). */}
          <div className="px-3 py-1 flex-1 min-h-[60px]">
            <FieldTextarea
              ref={textareaRef}
              value={value}
              onChange={onChange}
              onKeyDown={onKeyDown}
              onPaste={onPaste}
              placeholder={placeholder}
              disabled={disabled}
              highlight={highlight}
              className="w-full h-full resize-none"
            />
          </div>

          {/* Bottom row: AI selectors (left), mic + send/stop (right) */}
          <div className="px-3 pb-3 pt-1 flex-shrink-0">
            <div className="flex items-center justify-between gap-2">
              <div className="flex-1 min-w-0 flex items-center gap-2">
                {leftControls}
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <MicButton
                  recording={recording}
                  transcribing={transcribing}
                  onToggleRecording={onToggleRecording}
                />
                {/*
                  Plan chat-message-queue (kilocode model): one button, state derived from
                  composer emptiness. Empty + streaming → red Stop; non-empty → Send,
                  which enqueues while a turn streams (the store guard routes it to the
                  queue). No modality — mouse and the Cmd/Ctrl+Enter hotkey do the same.
                  Esc always Stops (see ChatInput.handleKeyDown) so a one-click stop is
                  still available whenever text is present.
                */}
                {isStreaming && onStop && value.trim().length === 0 ? (
                  <Button variant="danger" onClick={onStop}>
                    <Square size={14} className="mr-1" /> {t('stop')}
                  </Button>
                ) : (
                  <Button variant="primary" onClick={onSend} disabled={!canSend}>
                    <Send size={14} className="mr-1" /> {t('sendHotkey')}
                  </Button>
                )}              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
