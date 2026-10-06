/** Unified single chat message — store-agnostic. Renders user bubbles (dynamic
 * width, aligned by isOwn) and agent/assistant bubbles (full-width distinct
 * surface card). AI-only content (reasoning, the assembled turn,
 * sources) renders when present. All mutations arrive via `actions` callbacks;
 * the corresponding button is hidden when its callback is absent. */
// ARCH: store-agnostic — no useChatStore / useNoteChatStore calls inside. Each
// chat wrapper (MessageList for AI, NoteChatView for notes) reads its store and
// forwards data + actions. isOwn is an explicit prop (AI: all user messages are
// own; notes: author_id === currentUser.user_id).
// INVARIANT: note messages never carry AI content (reasoning/nodes/etc.) and
// note chat never starts an agent turn — the notes store simply never supplies those fields.  Why: note messages are user-authored only; the notes store never supplies AI fields, so note bubbles never render reasoning/tool chips or join a streaming turn.

import { memo, useCallback, useLayoutEffect, useRef, useState } from 'react';
import { Copy, Pencil, RefreshCw, GitFork, Check, X, Trash2, AlertTriangle, FileText } from 'lucide-react';
import { IconButton, FieldTextarea } from '../ui';
import { MarkdownContent } from './MarkdownContent';
import { ForkIndicator } from './ForkIndicator';
import { Sources } from './Sources';
import { VerdictCard } from './VerdictCard';
import { ChatMessage, HaltReason } from '../../types';
import { useTranslation } from '../../i18n';
import { ROOT_KEY } from '../../store/chat-store/tree';
import { formatDate } from '../../utils/format';
import { userColor } from '../../utils/user-color';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useLazyMessageImages } from './useLazyMessageImages';
// The bubble's internal helpers live in ./MessageBubble/internal/ (file + same-name
// dir convention, same as store/chat-store/sessions-slice.ts + sessions-slice/).
// Pure extraction — no behavioral change. NOTE: the dir is `internal/`, not `parts/`
// — the blanket `parts/` .gitignore rule (Python setuptools template) would ignore it.
import { ImageLightbox } from './MessageBubble/internal/image-lightbox';
import { HaltCard } from './MessageBubble/internal/halt-card';
import { TurnNodes, type TurnNodeLike } from './MessageBubble/internal/turn-nodes';

export interface MessageBubbleActions {
  onCopy?: (text: string) => void;
  onEdit?: (messageId: string, text: string) => void;
  onDelete?: (messageId: string) => void;
  onForkResend?: (messageId: string, text: string, images?: string[]) => void;
  onRegenerate?: (messageId: string) => void;
  onCreateDocument?: (content: string) => void;
  getSiblings?: (parentId: string | null) => ChatMessage[];
  selectSibling?: (parentKey: string, messageId: string) => void;
  /** The halt card's "Continue" — a visible user message
   *  through the existing sendMessage path (no new endpoint). Per-turn budgets
   *  reset naturally, and the transcript stays honest about why the agent
   *  restarted. Hidden when absent (the card renders read-only). */
  onContinue?: () => void;
  /** Agent chats: rewind-to-message — hide this message and everything below
   *  it; the next send forks a sibling from the point before it (the hidden
   *  branch stays in history). Hidden when absent (button never renders). */
  onRewind?: (messageId: string) => void;
}

interface Props {
  message: ChatMessage;
  variant: 'ai' | 'note';
  /** AI: all user messages are own. Notes: author_id === currentUser.user_id. */
  isOwn: boolean;
  /** Notes only: whether the current user is the project owner (root). The owner
   * may edit/delete any note message. AI chat ignores this. */
  isOwner?: boolean;
  /** Notes only: whether this message has replies (a non-leaf). A non-owner
   * author may NOT delete a non-leaf; the owner still can (cascade). */
  hasChildren?: boolean;
  isStreaming?: boolean;
  /** The turn's assembled nodes (see SYSTEM: dsh-conversation) — the assembler's
   * published data, sliced per message by MessageList). Present ⇒ the timeline
   * renders over node data and the row's segment/agent-step folds are not
   * rendered (they are the translator's product this path replaces). Empty for
   * frameless rows (pre-harness / abnormal turns) — those render from the
   * row's own fields as before. */
  nodes?: TurnNodeLike[];
  /** True when this bubble is the last entry in the active path. Forwarded to
   *  the segment timeline (the running plate / halt card may want it). */
  isLast?: boolean;
  // ARCH: AI (non-note) chats have no branching — Edit/Delete/Fork/Regenerate are
  // hidden. Computed once by MessageList and passed in to avoid a per-bubble
  // sessions.find() on every render.
  isAgent?: boolean;
  actions?: MessageBubbleActions;
}

// WHY memo: message list re-renders on every streaming delta; bubble props are stable unless message is edited
/**
 * Whether the streaming turn is waiting on the model with nothing to show:
 * before the first output, or after a tool settled and the next step has not
 * produced anything yet.
 *
 * INVARIANT: while a turn streams, SOMETHING on the bubble says the model is
 * working — the thinking line when no chip or text is arriving.
 * Why: the relay hides dsh's lifecycle and bookkeeping kinds, so the gap
 * between a settled tool and the next output carries no frame at all, and
 * without this the bubble reads as a finished answer mid-turn.
 */
function isAwaitingModel(message: ChatMessage): boolean {
  // Streaming text speaks for itself.
  return !message.content;
}

export const MessageBubble = memo(function MessageBubble({
  message,
  variant,
  isOwn,
  isOwner = false,
  hasChildren = false,
  isStreaming = false,
  nodes = [],
  isLast = false,
  isAgent = false,
  actions,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState('');
  const deleteAction = useArmedAction();
  const rewindAction = useArmedAction();
  const bubbleRef = useRef<HTMLDivElement>(null);
  const [bubbleW, setBubbleW] = useState<number>();
  const [openAttachIndex, setOpenAttachIndex] = useState<number | null>(null);
  const { t } = useTranslation();
  // Images are lazy-fetched (list_messages omits them, sends image_count).
  const { images: bubbleImages, count: imageCount, error: imagesError, ensureImages } =
    useLazyMessageImages(message);

  const isUser = message.role === 'user';
  // Note-chat own-only ACL (mirrors backend/access.py:can_modify_note_message).
  // AI chat ignores these — its gating is handled by isAgent.
  const isNote = variant === 'note';
  const noteCanEdit = isOwn || isOwner;
  const noteCanDelete = isOwner || (isOwn && !hasChildren);
  // Fork navigation is AI-only: getSiblings is undefined for notes, so the
  // indicator never renders there. Agent chats fork too (edit→branch): the user
  // message is the branch point, and the indicator switches between siblings.
  const siblings = actions?.getSiblings ? actions.getSiblings(message.parent_id) : [message];
  const hasForks = siblings.length > 1;
  const userFork = isUser && hasForks;

  // Measure the user bubble so the action row (ForkIndicator left / buttons
  // right) matches the bubble's actual width — the bubble is content-driven and
  // right-aligned, so the fork row's left edge must track it, not a fixed 85%.
  useLayoutEffect(() => {
    if (!isUser) return;
    const el = bubbleRef.current;
    if (!el) return;
    const sync = () => setBubbleW(el.offsetWidth);
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    return () => ro.disconnect();
  }, [isUser]);

  const handleCopy = useCallback(() => {
    // The parent owns clipboard + toast.
    actions?.onCopy?.(message.content);
  }, [message.content, actions]);

  const handleStartEdit = useCallback(() => {
    setEditText(message.content);
    setEditing(true);
  }, [message.content]);

  // Fork/resend must carry the images too — hydrate them first (they may not be
  // loaded yet since list_messages omits them), then dispatch.
  const forkResend = useCallback(async (text: string) => {
    setEditing(false);
    const imgs = await ensureImages();
    actions?.onForkResend?.(message.message_id, text, imgs.length ? imgs : undefined);
  }, [actions, message.message_id, ensureImages]);

  // Save: for an agent chat, saving an edit FORKS (a sibling branch with the edited
  // text) — in-place editing a replied-to user message would desync its reply, so
  // the only sensible save is a branch. Note chats save in-place (onEdit) as before.
  const handleSaveEdit = useCallback(() => {
    if (isAgent) {
      void forkResend(editText || message.content);
    } else {
      actions?.onEdit?.(message.message_id, editText);
      setEditing(false);
    }
  }, [isAgent, forkResend, actions, message.message_id, message.content, editText]);

  const handleForkResend = useCallback(() => {
    void forkResend(editText || message.content);
  }, [forkResend, editText, message.content]);

  const handleRegenerate = useCallback(() => {
    actions?.onRegenerate?.(message.message_id);
  }, [actions, message.message_id]);

  const handleCreateDocument = useCallback(() => {
    // The parent owns the apiClient/emit/toast side effects.
    actions?.onCreateDocument?.(message.content);
  }, [actions, message.content]);

  const currentIdx = siblings.findIndex(s => s.message_id === message.message_id);
  const parentKey = message.parent_id ?? ROOT_KEY;

  // Alignment: user+own → right; user+other / agent → left.
  const alignClass = isUser ? (isOwn ? 'items-end' : 'items-start') : 'items-start';

  // Theme map (variant + role + isOwn). NO rounded corners (project rule).
  // User messages: dynamic width + variant/role background.
  // Agent/assistant messages: full-width, base AI style (text-text px-0, NO outer
  // card). Why: reasoning widgets, Sources, proposals and agent-step chips each
  // carry their OWN bg-surface2/border card — wrapping them in an outer surface
  // card nests identical plates and hides the reasoning body (surface2-on-
  // surface2). This is the AI chat's established base style (commits c022558 /
  // 7f93fe5 deliberately dropped the outer plate); the unified bubble must keep it.
  const userBg = variant === 'note'
    ? isOwn
      ? 'bg-[var(--sticky-yellow-dark)] text-text'
      : 'text-text'
    : 'bg-[var(--sticky-yellow-dark)] text-text';
  // Other-user note messages get a transparent plaque marked with a 4px left
  // border in the author's deterministic presence color (matches their gutter
  // bar / chip across clients). The border is 80% transparent so it reads as a
  // subtle marker, not a hard accent. Own messages + AI chat are untouched. The
  // author_id guard avoids passing undefined to userColor (optional on
  // ChatMessage); a missing id simply yields an unmarked transparent bubble.
  const noteBorder = isNote && !isOwn && message.author_id
    ? { borderLeft: `4px solid ${userColor(message.author_id)}33` }
    : undefined;
  const bubbleClass = isUser
    ? `${editing ? 'w-full' : 'max-w-[85%] min-w-[200px]'} px-3 py-2 text-sm leading-relaxed ${userBg}`
    : 'w-full py-2 text-sm leading-relaxed text-text px-0';

  return (
    <div className={`flex flex-col mb-3 ${alignClass}`}>
      {!isUser && hasForks && (
        <ForkIndicator
          className="mb-1"
          current={currentIdx + 1}
          total={siblings.length}
          onPrev={() => {
            const prev = siblings[currentIdx - 1];
            if (prev) actions?.selectSibling?.(parentKey, prev.message_id);
          }}
          onNext={() => {
            const next = siblings[currentIdx + 1];
            if (next) actions?.selectSibling?.(parentKey, next.message_id);
          }}
        />
      )}

      {!isUser && message.sources && message.sources.length > 0 && !editing && (
        <div className="max-w-[85%]">
          <Sources sources={message.sources} />
        </div>
      )}
      {/* Author header — shared for user messages (note messages are all
       * user-role, so it shows for every note message; AI hides it on assistant). */}
      {isUser && (
        <div className="text-ui-2xs text-text-dim mb-0.5 px-1">
          {message.author_name ?? t('unknownAuthor')} | {formatDate(message.created_at)}
        </div>
      )}
      <div ref={bubbleRef} className={bubbleClass} style={noteBorder}>
        {editing ? (
          <div className="flex flex-col gap-2">
            <FieldTextarea
              value={editText}
              onChange={e => setEditText(e.target.value)}
              onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); handleSaveEdit(); } }}
              className="min-h-[60px] w-full"
              autoFocus
            />
            <div className="flex gap-1 justify-end">
              <IconButton size="sm" title={t('save')} onClick={handleSaveEdit}>
                <Check size={14} />
              </IconButton>
              {isUser && !isAgent && actions?.onForkResend && (
                <IconButton size="sm" title={t('forkAndResend')} onClick={handleForkResend}>
                  <GitFork size={14} />
                </IconButton>
              )}
              <IconButton size="sm" title={t('cancel')} onClick={() => setEditing(false)}>
                <X size={14} />
              </IconButton>
            </div>
          </div>
        ) : (
          <>
            {imageCount > 0 && (
              bubbleImages && bubbleImages.length > 0 ? (
                <div className="flex gap-2 mb-2 flex-wrap">
                  {bubbleImages.map((img, i) => (
                    // eslint-disable-next-line react/forbid-elements
                    <button key={i} type="button"
                      onClick={() => setOpenAttachIndex(i)}
                      aria-label={t('viewAttachmentImage')}
                      title={t('viewAttachmentImage')}
                      className="block border border-border cursor-pointer hover:opacity-80 transition-opacity">
                      <img src={img} alt={t('attachmentImage')}
                        className="max-w-[200px] max-h-[150px] object-contain block" />
                    </button>
                  ))}
                </div>
              ) : imagesError ? (
                // No silent degradation: a failed image fetch shows an explicit
                // error placeholder, distinct from the loading skeleton.
                <div className="mb-2 text-xs text-red-500 flex items-center gap-1">
                  <AlertTriangle size={12} />
                  {t('failedToLoadImages')}
                </div>
              ) : (
                <div className="flex gap-2 mb-2 flex-wrap">
                  {Array.from({ length: imageCount }).map((_, i) => (
                    <div key={i} className="w-[120px] h-[90px] bg-surface2 border border-border animate-pulse" />
                  ))}
                </div>
              )
            )}
            {/* Plan user-attachment-image-lightbox: the SAME src-based lightbox as
                generated images. Mounted only while an attachment is open, and only
                in the resolved-success branch (bubbleImages present), so it can never
                open against missing/loading images. */}
            {openAttachIndex !== null && bubbleImages && bubbleImages.length > 0 && (
              <ImageLightbox
                srcs={bubbleImages}
                index={openAttachIndex}
                onIndexChange={setOpenAttachIndex}
                onClose={() => setOpenAttachIndex(null)}
                title={t('attachmentImage')}
              />
            )}
            {isUser ? (
              // AI user messages are plain text; note messages render markdown
              // (notes persisted markdown via MarkdownContent historically).
              variant === 'note' ? (
                <MarkdownContent content={message.content} />
              ) : (
                <div className="whitespace-pre-wrap">{message.content}</div>
              )
            ) : nodes.length > 0 ? (
              // ARCH (SYSTEM: dsh-conversation): the assembled turn renders over
              // the node data — text, reasoning, tool chips, the lore cards, in
              // the assembler's order. When nodes exist `content` is NOT also
              // rendered (the row's content is the chat-list preview's copy of
              // the same text; rendering both would double it).
              <TurnNodes nodes={nodes} isStreaming={isStreaming} onContinue={actions?.onContinue} />
            ) : (
              // ARCH: the row's
              // `halt` column renders HERE — the no-nodes branch — because an
              // abnormally ended turn persists the card under `halt` and carries
              // no timeline. A turn WITH nodes carries its own lore/halt card, so
              // this branch is never a double render.
              <>
                <MarkdownContent content={message.content} streaming={isStreaming} />
                {message.halt && (
                  <HaltCard
                    // Same cast the live segment path makes: the column's reason is
                    // the backend's abnormal-end string, and an unknown reason
                    // renders degraded-but-visible (never silent).
                    reason={message.halt.reason as HaltReason}
                    steps={message.halt.steps}
                    limit={message.halt.limit}
                    // WHY: 0 — the tool recap counts the turn's calls, and a row
                    // reaching this branch has no timeline to count (the backend
                    // OMITs the retired columns; the node path counts its own).
                    toolCallCount={0}
                    onContinue={actions?.onContinue}
                  />
                )}
              </>
            )}
            {isStreaming && isAwaitingModel(message) && (
              <div className="text-text-dim text-xs animate-pulse">{t('thinking')}</div>
            )}
            {/* The verdict decision card: the lore/verdict-ask NODE renders it
                on the node timeline (the card stays once asked — see
                turn-nodes.tsx). This message-field render is the frameless
                fallback (GET /chat/verdicts restored holds on a row the log
                has no turn for). */}
            {!isUser && nodes.length === 0 && (message.pending_verdicts ?? []).length > 0 && (
              (message.pending_verdicts ?? []).map(p => (
                <div key={p.call_id} className="my-1"><VerdictCard pending={p} /></div>
              ))
            )}
            {message.unsaved && !isStreaming && (
              <div className="flex items-center gap-1 text-yellow-500 text-xs mt-1" title={t('chatSaveError')}>
                <AlertTriangle size={12} />
                <span>{t('chatSaveError')}</span>
              </div>
            )}
          </>
        )}
      </div>

      {/* Actions — always visible below the bubble */}
      {!editing && !isStreaming && (
        <div
          className={`flex items-center mt-0.5 ${
            isUser
              ? userFork
                ? 'justify-between'
                : 'justify-end'
              : 'justify-start'
          }`}
          style={userFork ? { width: bubbleW } : undefined}
        >
          {userFork && (
            <ForkIndicator
              current={currentIdx + 1}
              total={siblings.length}
              onPrev={() => {
                const prev = siblings[currentIdx - 1];
                if (prev) actions?.selectSibling?.(parentKey, prev.message_id);
              }}
              onNext={() => {
                const next = siblings[currentIdx + 1];
                if (next) actions?.selectSibling?.(parentKey, next.message_id);
              }}
            />
          )}
          <div className="flex gap-0.5">
            {!isUser && actions?.onCreateDocument && (
              <IconButton size="sm" title={t('createDocumentFromChat')} onClick={handleCreateDocument}>
                <FileText size={12} />
              </IconButton>
            )}
            {actions?.onCopy && (
              <IconButton size="sm" theme={variant === 'note' ? 'note' : undefined} title={t('copy')} onClick={handleCopy}>
                <Copy size={12} />
              </IconButton>
            )}
            {isUser && !isAgent && actions?.onDelete && (!isNote || noteCanDelete) && (
              <IconButton
                size="sm"
                danger
                filled={deleteAction.armed}
                theme={variant === 'note' ? 'note' : undefined}
                title={t('deleteBranch')}
                onClick={() => deleteAction.handleClick(() => actions.onDelete?.(message.message_id))}
                onMouseLeave={deleteAction.disarm}
              >
                <Trash2 size={12} />
              </IconButton>
            )}
            {/* Edit: agent chats edit→fork on save, USER messages only (editing an
             * agent reply is not supported — in-place edit would desync, and re-run
             * is the separate, out-of-scope regenerate). Note chats edit in-place. */}
            {(isAgent ? (isUser && actions?.onForkResend) : (actions?.onEdit && (!isNote || noteCanEdit))) && (
              <IconButton size="sm" theme={variant === 'note' ? 'note' : undefined} title={t('edit')} onClick={handleStartEdit}>
                <Pencil size={12} />
              </IconButton>
            )}
            {/* Rewind (agent user messages only): hide this message and
             * everything below it — the next send forks a sibling before it.
             * Armed double-click like delete, and styled like it: IconButton's
             * `filled` armed state only shows with a colour, so without `danger`
             * the first click looked like it did nothing. */}
            {isUser && isAgent && actions?.onRewind && (
              <IconButton
                size="sm"
                danger
                filled={rewindAction.armed}
                title={t('rewindHere')}
                onClick={() => rewindAction.handleClick(() => actions.onRewind?.(message.message_id))}
                onMouseLeave={rewindAction.disarm}
              >
                <Trash2 size={12} />
              </IconButton>
            )}
            {isUser && !isAgent && actions?.onForkResend && (
              <IconButton size="sm" title={t('forkAndResend')} onClick={() => void forkResend(message.content)}>
                <GitFork size={12} />
              </IconButton>
            )}
            {!isUser && !isAgent && actions?.onRegenerate && (
              <IconButton size="sm" title={t('regenerate')} onClick={handleRegenerate}>
                <RefreshCw size={12} />
              </IconButton>
            )}
          </div>
        </div>
      )}
    </div>
  );
});
