/**
 * Floating "Clarify" affordance over a text selection inside an assistant message.
 *
 * ARCH: Bridges a selection in a `.chat-markdown` bubble to the composer via the
 * global event bus (`chat-clarify-insert`) — the same mechanism MarkdownContent
 * already uses for in-app navigation. No shared store state: the draft lives in
 * ChatInput's local `text`, and this component only emits quote + question.
 *
 * Mount once (in ChatPanel). Reads the native DOM Selection directly (no CM6),
 * positions via portal + post-measure clamp, mirroring SelectionToolbar.
 */

import { useState, useEffect, useCallback, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { MessageSquareQuote, Send } from 'lucide-react';
import { Button, FieldTextarea } from '../ui';
import { emit } from '../../events';
import { useTranslation } from '../../i18n';
import { useSimpleVoiceRecording } from '../../hooks/useSimpleVoiceRecording';
import { MicButton } from './shared/MicButton';

interface Anchor {
  centerX: number;
  belowTop: number;
  aboveBottom: number;
}

/** Returns the trimmed selection text if it sits inside a single `.chat-markdown`, else null. */
function selectionInChatMarkdown(): { text: string; rect: DOMRect } | null {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const text = sel.toString().trim();
  if (!text) return null;
  const range = sel.getRangeAt(0);
  const container = range.commonAncestorContainer;
  const el = container.nodeType === Node.ELEMENT_NODE ? (container as Element) : container.parentElement;
  if (!el || !el.closest('.chat-markdown')) return null;
  const rect = range.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { text, rect };
}

export function ChatClarifyPopover() {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);
  const [anchor, setAnchor] = useState<Anchor>({ centerX: 0, belowTop: 0, aboveBottom: 0 });
  const [asking, setAsking] = useState(false);
  const [question, setQuestion] = useState('');
  // Frozen at the moment the button is shown — focusing the form collapses the DOM selection.
  const quoteRef = useRef('');
  const popoverRef = useRef<HTMLDivElement>(null);

  // Transcription APPENDS to the local question (never overwrites — the user may
  // have typed more). Mirrors the chat composer's read-append-write pattern.
  const onTranscribed = useCallback((text: string) => {
    setQuestion(prev => prev + (prev ? ' ' : '') + text);
  }, []);
  const { recording, transcribing, toggleRecording } = useSimpleVoiceRecording(onTranscribed);

  const hide = useCallback(() => {
    // INVARIANT: while the mic records or transcribes, the popover must not be
    // dismissible (Esc, outside click and Cancel all route here).
    // Why: hide() clears `question`, and the in-flight /api/chat/transcribe
    // still resolves into setQuestion afterwards — the user's speech would land
    // in a cleared state and silently vanish on the next showFor().
    if (recording || transcribing) return;
    setVisible(false);
    setAsking(false);
    setQuestion('');
    quoteRef.current = '';
  }, [recording, transcribing]);

  const showFor = useCallback((text: string, rect: DOMRect) => {
    quoteRef.current = text;
    setAnchor({
      centerX: rect.left + rect.width / 2,
      belowTop: rect.bottom + 6,
      aboveBottom: rect.top - 6,
    });
    setAsking(false);
    setQuestion('');
    setVisible(true);
  }, []);

  // Detect selection on mouseup anywhere; show only for selections inside a chat bubble.
  useEffect(() => {
    const onMouseUp = (e: MouseEvent) => {
      // Ignore clicks inside the popover itself (button/form interactions).
      if (popoverRef.current?.contains(e.target as Node)) return;
      const found = selectionInChatMarkdown();
      if (found) showFor(found.text, found.rect);
      else if (!asking) hide();
    };
    document.addEventListener('mouseup', onMouseUp);
    return () => document.removeEventListener('mouseup', onMouseUp);
  }, [showFor, hide, asking]);

  // Esc dismisses.
  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') hide(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [visible, hide]);

  // Click outside dismisses.
  useEffect(() => {
    if (!visible) return;
    const onDown = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) hide();
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [visible, hide]);

  // Post-measure placement: flip above when no room below, clamp horizontally.
  useLayoutEffect(() => {
    const el = popoverRef.current;
    if (!visible || !el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let top = anchor.belowTop;
    const spaceBelow = window.innerHeight - anchor.belowTop;
    if (spaceBelow < h + 8 && anchor.aboveBottom - h >= 8) top = anchor.aboveBottom - h;
    top = Math.max(8, Math.min(top, window.innerHeight - h - 8));
    const half = w / 2;
    let center = anchor.centerX;
    if (center - half < 8) center = half + 8;
    if (center + half > window.innerWidth - 8) center = window.innerWidth - 8 - half;
    el.style.left = `${center}px`;
    el.style.top = `${top}px`;
  }, [visible, anchor, asking]);

  const handleConfirm = useCallback(() => {
    // Same busy guard as hide: confirming mid-recording would emit a partial
    // question while the popover (per the hide guard) stays open.
    if (recording || transcribing) return;
    const q = question.trim();
    if (!q || !quoteRef.current) return;
    emit('chat-clarify-insert', { quote: quoteRef.current, question: q });
    hide();
  }, [question, hide, recording, transcribing]);

  if (!visible) return null;

  return createPortal(
    <div
      ref={popoverRef}
      className="fixed left-0 top-0 -translate-x-1/2 z-[45] bg-surface border border-border shadow-lg p-1"
      onMouseDown={e => { if (!asking) e.preventDefault(); }}
    >
      {!asking ? (
        <Button variant="primary" size="sm" onClick={() => setAsking(true)}>
          <MessageSquareQuote size={14} className="mr-1" />
          {t('chatClarify')}
        </Button>
      ) : (
        <div className="flex flex-col gap-2 w-[280px]">
          <FieldTextarea
            autoFocus
            value={question}
            onChange={e => setQuestion(e.target.value)}
            onKeyDown={e => {
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); handleConfirm(); }
            }}
            placeholder={t('chatClarifyPlaceholder')}
            className="w-full h-20 resize-none"
          />
          <div className="flex justify-end items-center gap-1">
            <Button variant="ghost" size="md" onClick={hide}>{t('cancel')}</Button>
            <MicButton
              recording={recording}
              transcribing={transcribing}
              onToggleRecording={toggleRecording}
            />
            <Button
              variant="primary"
              onClick={handleConfirm}
              disabled={!question.trim() || recording || transcribing}
            >
              <Send size={14} className="mr-1" /> {t('sendHotkey')}
            </Button>
          </div>
        </div>
      )}
    </div>,
    document.body,
  );
}
