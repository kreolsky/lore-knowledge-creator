/** Scrollable message list — renders the branch path from the message tree. Chat-store slices: selectBranchPath, streaming, messagesLoading, activeSessionId. */

import { useEffect, useRef, useMemo, useLayoutEffect, useCallback } from 'react';
import { MessageBubble, type MessageBubbleActions } from './MessageBubble';
import type { ForkNav } from './MessageBubble';
import { ChatEmptyList } from './ChatEmptyList';
import { useChatStore, selectBranchPath } from '../../store/chat-store';
import type { ChatMessage } from '../../types';
import type { TurnNodeLike } from './MessageBubble/internal/turn-nodes';
import { useAppStore } from '../../store/app-store';
import { useTranslation, t as translate } from '../../i18n';
import { PanelLoading, Button } from '../ui';
import { apiClient } from '../../api/client';
import { emit, on, off } from '../../events';
import type { Document } from '../../types';
import { copyWithToast } from './shared/copy';

// Distance from bottom (px) below which we consider the user "at bottom" and keep
// auto-scrolling. Must be large enough to tolerate temporary scroll-height spikes
// (mid-stream content renders faster than scrollTo catches up), but small enough
// that a deliberate user scroll-up disengages the stick quickly.
// NOTE(plan reasoning-growth-stops-shaking-tool-chips): the instant follow branch
// removed the catch-up window this epsilon was tuned for; narrowing it is a
// separate task.
const STICK_EPSILON = 80;

// Content-space top of `anchor` relative to the scroll container's content box.
// Rect deltas normalized by scrollTop cancel user scrolling between passes and do
// not depend on which ancestor happens to be the anchor's offsetParent.
function anchorContentTop(el: HTMLElement, anchor: HTMLElement): number {
  return anchor.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
}

// Bottom-most visible element: descend the LAST child chain, skipping children
// below the fold, into the deepest one that has layout. Anchoring here (not the
// topmost visible element) is what compensates growth that happens ABOVE the
// reading position: the plate appends just above the chip the user reads, and a
// topmost-visible anchor sits above that growth point, so its delta would
// always be 0 (measured live, plan reasoning-growth-stops-shaking-tool-chips).
function pickAnchor(el: HTMLElement): HTMLElement | null {
  const elRect = el.getBoundingClientRect();
  const viewBottom = elRect.top + el.clientHeight;
  const descend = (parent: HTMLElement): HTMLElement | null => {
    const children = parent.children;
    for (let i = children.length - 1; i >= 0; i--) {
      const child = children[i] as HTMLElement;
      const r = child.getBoundingClientRect();
      if (r.top >= viewBottom) continue; // below the fold — try earlier siblings
      if (r.bottom - r.top <= 0) continue; // no layout — nothing to pin (also keeps jsdom stubs at the stubbed level)
      const deeper = child.children.length > 0 ? descend(child) : null;
      return deeper ?? child;
    }
    return null;
  };
  return descend(el);
}

export function MessageList() {
  const { t } = useTranslation();
  // The branch path: the freshest lineage over the session's rows (a branch
  // session's rows are a linear chain — the whole chain renders).
  const basePath = useChatStore(selectBranchPath);
  const streaming = useChatStore(s => s.streaming);
  const isStreaming = streaming !== null;
  const streamingMessageId = streaming?.messageId ?? null;
  const streamingContent = streaming?.content ?? '';
  // The assembler's published timeline (SYSTEM: dsh-conversation) + the
  // placement the renderer resolves against.
  const conversation = useChatStore(s => s.conversation);
  const turnRanges = useChatStore(s => s.turnRanges);
  const turnStartSeq = useChatStore(s => s.turnStartSeq);
  // The detached image run's live facts ride the CHAT channel (chat_frame
  // lore/image-gen envelopes → dispatchChatFrame → the assembler's image-gen
  // node renders its own running phase) — no project-WS image events, no
  // store-side phase map.
  const messagesLoading = useChatStore(s => s.messagesLoading);
  const messagesError = useChatStore(s => s.messagesError);
  const chatScopeLoading = useChatStore(s => s.chatScopeLoading);
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const loadMessages = useChatStore(s => s.loadMessages);
  // The read keys on the is_note axis — every non-note (AI) session is an agent
  // session; note chats (handled by a different composer) never reach
  // here, but the flag stays the honest "is this an AI chat" read.
  const isAgent = useChatStore(s =>
    !s.sessions.find(ss => ss.session_id === s.activeSessionId)?.is_note
  );

  // SYSTEM: chat-branch-sessions — the switcher's projection for the ACTIVE
  // session (its branches' fork points), reloaded on every session switch.
  const forks = useChatStore(s => s.forks);
  const loadForks = useChatStore(s => s.loadForks);
  const openBranch = useChatStore(s => s.openBranch);
  const messages = useChatStore(s => s.messages);
  useEffect(() => {
    if (activeSessionId) void loadForks(activeSessionId);
  }, [activeSessionId, loadForks]);

  // ARCH: store-agnostic MessageBubble receives all mutations via `actions`.
  // The bundle is memoized so the memo'd bubble props stay stable across
  // streaming-delta re-renders (zustand action refs are stable). handleCopy must
  // NOT depend on the hook `t` (fresh per render) — it uses the standalone
  // copyWithToast, else the whole list would re-render per streaming token.
  const showToast = useAppStore(s => s.showToast);
  const editMessage = useChatStore(s => s.editMessage);
  const forkAndResend = useChatStore(s => s.forkAndResend);
  const regenerate = useChatStore(s => s.regenerate);
  // Rewind-to-message: forks a branch ending right before the message and
  // opens it (the old line stays reachable through the switcher).
  const rewindTo = useChatStore(s => s.rewindTo);

  const handleCopy = useCallback((text: string) => {
    // A settled agent row whose `done` frame never carried text (or a
    // tool-only turn) would copy an empty string — say so instead. The
    // standalone translate (not the hook `t`) keeps the deps at [showToast]
    // so the list does not re-render per streaming token.
    if (!text.trim()) {
      showToast(translate('replyTextNotReady'), 'warning');
      return;
    }
    copyWithToast(text, showToast);
  }, [showToast]);

  const handleCreateDocument = useCallback((content: string) => {
    if (!content.trim()) {
      showToast(translate('replyTextNotReady'), 'warning');
      return;
    }
    const { currentProject, currentDocument, setDocuments } = useAppStore.getState();
    if (!currentProject || !currentDocument) return;
    apiClient.post('/documents', {
      project_id: currentProject.project_id,
      parent_id: currentDocument.document_id,
      content,
    }).then((newDoc: Document) => {
      const freshDocs = useAppStore.getState().documents;
      if (!freshDocs.some(d => d.document_id === newDoc.document_id)) {
        setDocuments([...freshDocs, newDoc]);
      }
      emit('navigate-to-document', { documentId: newDoc.document_id });
    }).catch(() => {
      showToast(t('failedToCreateDocument'), 'error');
    });
  }, [showToast]);

  // The halt card's "Continue" is a VISIBLE user message
  // through the existing sendMessage path — no new endpoint, per-turn budgets reset
  // naturally, and the transcript stays honest about why the agent restarted.
  // Rejected: the invisible resume used by applyProposal (the history would show a
  // continuation with no cause).
  const handleContinue = useCallback(() => {
    void useChatStore.getState().sendMessage(t('chatHaltContinue'));
  }, [t]);

  const actions = useMemo<MessageBubbleActions>(() => ({
    onCopy: handleCopy,
    onEdit: editMessage,
    onForkResend: forkAndResend,
    onRegenerate: regenerate,
    onCreateDocument: handleCreateDocument,
    onContinue: handleContinue,
    onRewind: messageId => { void rewindTo(messageId); },
  }), [handleCopy, editMessage, forkAndResend, regenerate, handleCreateDocument, handleContinue, rewindTo]);

  // ARCH: Streaming substitution lives here (not in the selector) because
  // useSyncExternalStore requires getSnapshot to return a stable reference.
  // useMemo caches naturally per React's rules.
  const activePath = useMemo(() => {
    if (!streamingMessageId || !streamingContent) return basePath;
    return basePath.map(m => {
      if (m.message_id !== streamingMessageId) return m;
      const next = { ...m };
      if (streamingContent) next.content = streamingContent;
      return next;
    });
  }, [basePath, streaming]);

  // Place the switcher (see SYSTEM: chat-branch-sessions): for every fork
  // entry (origin P + the thread's distinct continuations after P, this
  // session's own flagged `current`), the indicator sits on the row whose
  // PARENT's origin is P. "k/N" is the server's order — the same for every
  // branch of the thread.
  const forkNavByRow = useMemo(() => {
    const out = new Map<string, ForkNav>();
    if (!activeSessionId || forks.length === 0) return out;
    const byId = new Map(messages.map(m => [m.message_id, m] as const));
    const originOf = (m: ChatMessage) => m.origin_id ?? m.message_id;
    for (const entry of forks) {
      const idx = entry.options.findIndex(o => o.current);
      if (idx < 0) continue;
      const row = messages.find(m => {
        const parent = m.parent_id ? byId.get(m.parent_id) : undefined;
        return (parent ? originOf(parent) : null) === entry.after_origin;
      });
      if (!row) continue;
      const open = (i: number) => {
        const target = entry.options[i];
        if (target && !target.current) openBranch(target.session_id);
      };
      out.set(row.message_id, {
        current: idx + 1,
        total: entry.options.length,
        onPrev: () => open(idx - 1),
        onNext: () => open(idx + 1),
      });
    }
    return out;
  }, [forks, messages, activeSessionId, openBranch]);

  // Slice the published nodes per assistant row (see SYSTEM: dsh-conversation).
  // The live turn owns the nodes above `turnStartSeq`; a settled row owns the
  // nodes inside its turn window (inclusive both ends — windows are disjoint).
  // A slice's array reference is REUSED when the slice did not change, so a
  // streaming publication re-renders only the streaming bubble (the memoized
  // MessageBubble compares props shallowly).
  const prevSlicesRef = useRef<Map<string, TurnNodeLike[]>>(new Map());
  const nodesByMessage = useMemo(() => {
    const map = new Map<string, TurnNodeLike[]>();
    for (const node of conversation) {
      let owner: string | null = null;
      if (turnStartSeq !== null && streamingMessageId && node.anchorSeq > turnStartSeq) {
        owner = streamingMessageId;
      } else {
        for (const [mid, range] of Object.entries(turnRanges)) {
          if (node.anchorSeq >= range.min && node.anchorSeq <= range.max) { owner = mid; break; }
        }
      }
      if (!owner) continue;
      const list = map.get(owner);
      if (list) list.push(node); else map.set(owner, [node]);
    }
    for (const [mid, list] of map) {
      const prev = prevSlicesRef.current.get(mid);
      // INVARIANT: the slice is reused only when EVERY node is the same object.
      // Why: the feed publishes a memoized VM per assembler node, so identity
      // is what "did not change" means here. A coarser test freezes the
      // streaming row — a growing assistant step keeps its length AND its key
      // while its `data` grows, so a length+key comparison reuses the stale
      // slice and the text stops advancing mid-turn.
      if (prev && prev.length === list.length && prev.every((n, i) => n === list[i])) {
        map.set(mid, prev);
      }
    }
    prevSlicesRef.current = map;
    return map;
  }, [conversation, turnRanges, turnStartSeq, streamingMessageId]);

  const containerRef = useRef<HTMLDivElement>(null);
  // INVARIANT: stickToBottomRef is the single source of truth for "follow the stream" — it picks
  // the scroll branch (stuck → instant snap; not stuck → element anchor) in the layout effect.
  // Why: auto-scroll keys off this ref (user scroll disables it); reset to true on session
  // switch / messages load so a new stream re-follows.
  const stickToBottomRef = useRef(true);
  // Anchor for the not-stuck branch: the pinned element + its content-space top
  // from the previous layout pass (null = pick on the next pass).
  const anchorElRef = useRef<HTMLElement | null>(null);
  const anchorTopRef = useRef<number | null>(null);
  // Path length at the previous layout pass — a change is a message boundary
  // (new message / user turn) owned by the smooth glide effect below.
  const prevPathLenRef = useRef<number | null>(null);

  // Last "scrolled away" value broadcast to the composer's scroll-to-bottom button.
  // WHY: a ref + event, not state — a scroll must not re-render the message list.
  const awayRef = useRef(false);
  const setAway = useCallback((away: boolean) => {
    if (awayRef.current === away) return;
    awayRef.current = away;
    emit('chat-scrolled-away', { away });
  }, []);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < STICK_EPSILON;
    // WHY: the scroll-to-bottom button shows exactly when auto-follow is off — one
    // threshold for both, no separate button threshold (operator requirement).
    setAway(!stickToBottomRef.current);
  }, [setAway]);

  useEffect(() => () => setAway(false), [setAway]);

  useEffect(() => {
    const jump = () => {
      const el = containerRef.current;
      if (!el) return;
      // WHY: instant, not smooth — a smooth glide emits mid-way scroll events above the
      // threshold (re-unsticking) and is cancelled by the anchor branch's scrollTop writes.
      el.scrollTop = el.scrollHeight;
      stickToBottomRef.current = true;
      anchorElRef.current = null;
      anchorTopRef.current = null;
      setAway(false);
    };
    on('chat-scroll-to-bottom', jump);
    return () => off('chat-scroll-to-bottom', jump);
  }, [setAway]);

  // Instant snap to bottom on session switch or after messages finish loading.
  // useLayoutEffect runs synchronously before paint — user never sees a "starts at top, scrolls down" animation.
  useLayoutEffect(() => {
    // A new session / reload starts at the bottom — and a list too short to scroll
    // fires no scroll event that would clear the button.
    setAway(false);
    if (messagesLoading) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    stickToBottomRef.current = true;
  }, [activeSessionId, messagesLoading, setAway]);

  // ARCH: one layout effect owns scroll position, replacing the old smooth
  // follow (plan reasoning-growth-stops-shaking-tool-chips). No dep array — it
  // must run after every commit that changed layout, before paint.
  useLayoutEffect(() => {
    // An empty / error state renders no scroll container — nothing to jump to, so
    // the button must not linger from a scroll made before the list went away.
    if (!containerRef.current) setAway(false);
    // A path-length change is a message boundary: the keyed effect below owns it
    // with a smooth glide — skip this pass so the glide is actually visible.
    if (prevPathLenRef.current !== null && activePath.length !== prevPathLenRef.current) {
      prevPathLenRef.current = activePath.length;
      anchorElRef.current = null;
      anchorTopRef.current = null;
      return;
    }
    prevPathLenRef.current = activePath.length;
    const el = containerRef.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      // WHY: a smooth follow here made the tool chips oscillate — reasoning
      // growth shifted layout down synchronously while the smooth scrollTo
      // pulled back up over the next few hundred ms, re-targeted from its
      // middle by every delta. The instant write paints growth in place.
      el.scrollTop = el.scrollHeight;
      anchorElRef.current = null;
      anchorTopRef.current = null;
      return;
    }
    // Not stuck: keep the bottom-most visible element pinned. Never use
    // scrollHeight deltas here — growth BELOW the viewport would drag the view
    // down on every appended chip.
    let anchor = anchorElRef.current;
    if (!anchor || !anchor.isConnected) {
      // A published node can replace the element the anchor pointed at, so the
      // stored anchor can unmount between passes — re-pick.
      anchor = pickAnchor(el);
      anchorElRef.current = anchor;
      anchorTopRef.current = anchor ? anchorContentTop(el, anchor) : null;
      return;
    }
    const stored = anchorTopRef.current;
    if (stored === null) {
      anchorTopRef.current = anchorContentTop(el, anchor);
      return;
    }
    const delta = anchorContentTop(el, anchor) - stored;
    if (delta !== 0) el.scrollTop += delta;
    anchorTopRef.current = anchorContentTop(el, anchor);
  });

  // Smooth glide on a message boundary only (new entry in the active path — new
  // message, user turn). Intra-message stream growth is instant via the layout
  // effect above; only this per-boundary transition stays animated.
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [activePath.length]);

  // INVARIANT: one continuous spinner across scope-change → messages-loaded. Gate
  // the empty ("start conversation") state behind BOTH flags so the "no chats"
  // empty state never flashes during loadSessions → resolve → loadMessages. Why:
  // chatScopeLoading covers the entry→resolve window (activeSessionId cleared but
  // messagesLoading still false), messagesLoading covers the fetch itself.
  if (chatScopeLoading || messagesLoading) {
    return <PanelLoading />;
  }

  // Distinct error state (no-silent-degradation): a load failure must NOT masquerade
  // as the "no messages" empty list once the toast dismisses. Offer a retry.
  if (messagesError) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 px-4 text-center">
        <p className="text-sm text-text-dim">{t('chatMessagesLoadFailed')}</p>
        {activeSessionId && (
          <Button variant="ghost" size="sm" onClick={() => void loadMessages(activeSessionId)}>
            {t('retry')}
          </Button>
        )}
      </div>
    );
  }

  // An empty conversation (fresh branch included) is the chat picker, not an
  // error state.
  if (activePath.length === 0) {
    return <ChatEmptyList />;
  }

  // WHY: overflow-anchor must be OFF on this container — the browser's native
  // scroll anchoring picks a node near the viewport top and fights both manual
  // branches (live drive, plan reasoning-growth-stops-shaking-tool-chips: with
  // it on, the stuck bottom pin drifted ~1px/frame and the not-stuck manual
  // anchor double-corrected by ~62px per pass).
  return (
    <div ref={containerRef} onScroll={handleScroll} className="[overflow-anchor:none] flex-1 overflow-y-auto px-3 py-3">
      {activePath.map((msg, idx) => (
        <MessageBubble
          key={msg.message_id}
          message={msg}
          variant="ai"
          isOwn={msg.role === 'user'}
          isStreaming={isStreaming && msg.message_id === streamingMessageId}
          nodes={nodesByMessage.get(msg.message_id)}
          isLast={idx === activePath.length - 1}
          isFirst={idx === 0}
          forkNav={forkNavByRow.get(msg.message_id)}
          isAgent={isAgent}
          actions={actions}
        />
      ))}
    </div>
  );
}
