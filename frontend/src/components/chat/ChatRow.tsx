/** Shared two-line chat session row — rendered in the empty-screen list. Labels the row with its parent entity: a document-session shows its document title (backend document_title); a reference-scoped chat shows `ref: <reference title>` (its parent IS the reference). The label is clickable → navigates to that document/reference; the row body click → opens the chat. */

import { memo, useMemo, useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import type { ChatSession } from '../../types';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { readRefOpenMode, refIsScope } from '../../store/ui-store/documents-slice';
import { emit } from '../../events';
import { useTranslation } from '../../i18n';
import { formatDate } from '../../utils/format';
import { FieldInput, IconButton, ListPill, RowActions } from '../ui';
import { useArmedAction } from '../../hooks/useArmedAction';
import { ListRow } from './ListRow';

interface ChatRowProps {
  session: ChatSession;
  isActive?: boolean;
  onSelect: (id: string) => void;
  // Optional armed-delete action (ChatEmptyList passes it; standalone usage omits).
  onDelete?: (id: string) => void;
  // Optional inline rename (ChatEmptyList passes it; standalone usage omits). The
  // row owns the editing state; the parent owns the store write. Resolves after the
  // server confirms so the input closes only once the title is persisted.
  onRename?: (id: string, title: string) => Promise<void>;
  // Optional parent-pill hover preview (ChatEmptyList owns the single popup; standalone
  // usage omits). onParentHover receives the parent entity id + the hovered span element
  // + isRef so the list can anchor + lazy-fetch the right preview: a document body
  // (useDocumentPreview) or a reference body/image (useReferencePreview — shared scheme).
  onParentHover?: (id: string, el: HTMLElement, isRef: boolean) => void;
  onParentHoverLeave?: (e?: React.MouseEvent) => void;
  // Optional last-message hover preview on the whole plaque (ChatEmptyList owns the
  // single popup). onBodyHover receives the session id + the hovered pill element so
  // the list can anchor + lazy-fetch the last message.
  onBodyHover?: (sessionId: string, el: HTMLElement) => void;
  onBodyHoverLeave?: (e?: React.MouseEvent) => void;
}

export const ChatRow = memo(function ChatRow({ session, isActive, onSelect, onDelete, onRename, onParentHover, onParentHoverLeave, onBodyHover, onBodyHoverLeave }: ChatRowProps) {
  const { t } = useTranslation();
  const references = useAppStore(s => s.references);
  const documents = useAppStore(s => s.documents);
  const currentDocumentId = useAppStore(s => s.currentDocument?.document_id);
  const rawReferenceId = useAppStore(s => s.currentReference?.reference_id);
  // Panel quick preview: the doc is the chat's scope, so a ref-session is NOT
  // "current" while its ref is merely previewed — the goto-ref affordance shows.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocumentId ?? '']));
  const currentReferenceId = refIsScope(refOpenMode) ? rawReferenceId : null;
  const deleteAction = useArmedAction();
  // Inline rename — same contract as the reference plaque (RefCard): pencil or
  // double-click opens the input, Enter / blur (click anywhere) commits, Escape cancels.
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');

  const startRename = (e: React.MouseEvent) => {
    e.stopPropagation();
    setRenameValue(session.title || '');
    setRenaming(true);
  };

  const commitRename = async () => {
    const trimmed = renameValue.trim();
    if (!onRename || !trimmed || trimmed === session.title) { setRenaming(false); return; }
    // Close AFTER the server confirms (the parent's optimistic set paints the new title
    // at once); updateSession owns the error toast and never rejects, finally is the
    // guard that a failed PATCH still releases the input.
    try {
      await onRename(session.session_id, trimmed);
    } finally {
      setRenaming(false);
    }
  };

  // INVARIANT: a reference-scoped chat's parent IS the reference (`ref: <title>`),
  // never its owning document — even when created in split view. Why: the chat's
  // identity is the entity it was created on (a recurring user rule).
  // For a document-session the label is the backend document_title (its own title),
  // falling back to the client document tree; the reference title comes from the
  // backend reference_title, falling back to the client reference list.
  const isRef = !!session.reference_id;
  const rawTitle = useMemo<string | null>(() => {
    if (isRef) {
      return session.reference_title
        ?? references.find(r => r.reference_id === session.reference_id)?.title
        ?? null;
    }
    if (session.document_title) return session.document_title;
    if (session.document_id) {
      return documents.find(d => d.document_id === session.document_id)?.title ?? null;
    }
    return null;
  }, [isRef, session.reference_title, session.document_title, session.document_id, session.reference_id, references, documents]);
  const parentLabel = rawTitle == null
    ? null
    : (isRef ? t('refParentLabel', { title: rawTitle }) : rawTitle);

  // The navigation target: the reference for a ref-session, else the owning doc.
  // Clickable only when it points at a DIFFERENT open entity (a no-op otherwise).
  const parentDocId = isRef ? (session.reference_id ?? null) : (session.document_id ?? null);
  const parentClickable = isRef
    ? !!session.reference_id && session.reference_id !== currentReferenceId
    : !!parentDocId && parentDocId !== currentDocumentId;

  // ARCH: show the last USER message
  // time (the backend aggregate scans role='user' only), falling back to the
  // immutable created_at — NEVER updated_at. Why: updated_at is bumped by
  // auto_title / update_session, which would make the card date jump on
  // enter→leave / rename. last_message_at is null only for a freshly-created
  // row before its first user message; created_at then anchors the date.
  const dateStr = useMemo(
    () => formatDate(session.last_message_at ?? session.created_at),
    [session.last_message_at, session.created_at],
  );

  // ARCH: route navigation through the canonical event (handled in Header.tsx) so
  // BOTH the store and the URL/route update — otherwise the left-panel highlight,
  // derived from useParams(), stays on the previously open entity. A ref-session
  // navigates to the REFERENCE (navigate-to-reference), never its owning document.
  const emitNavigate = () => {
    if (!parentClickable || !parentDocId) return;
    if (isRef) emit('navigate-to-reference', { referenceId: parentDocId });
    else emit('navigate-to-document', { documentId: parentDocId });
  };

  const handleParentClick = (e: React.MouseEvent) => {
    if (!parentClickable || !parentDocId) return;
    e.stopPropagation();
    emitNavigate();
  };

  const handleParentKeyDown = (e: React.KeyboardEvent) => {
    if (!parentClickable || !parentDocId) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      emitNavigate();
    }
  };

  return (
    <ListPill
      active={isActive}
      activeColor="accent"
      onClick={() => onSelect(session.session_id)}
      onMouseEnter={onBodyHover ? (e) => onBodyHover(session.session_id, e.currentTarget as HTMLElement) : undefined}
      onMouseLeave={onBodyHoverLeave}
    >
      <ListRow
        body={session.title || t('newChat')}
        bodyOverride={renaming ? (
          <FieldInput
            className="ref-rename-input"
            data-rename-input
            value={renameValue}
            onChange={e => setRenameValue(e.target.value)}
            autoFocus
            onKeyDown={e => {
              if (e.key === 'Enter') { e.preventDefault(); void commitRename(); }
              if (e.key === 'Escape') { setRenaming(false); }
            }}
            onBlur={() => { void commitRename(); }}
            onClick={e => e.stopPropagation()}
          />
        ) : undefined}
        onBodyDoubleClick={onRename ? startRename : undefined}
        meta={
          parentLabel ? (
            <>
              {dateStr} |{' '}
              {parentClickable ? (
                <span
                  role="button"
                  tabIndex={0}
                  className="text-text-muted hover:text-accent underline-offset-2 hover:underline cursor-pointer"
                  title={t('gotoParentDoc', { title: parentLabel })}
                  onClick={handleParentClick}
                  onKeyDown={handleParentKeyDown}
                  onMouseEnter={onParentHover && parentDocId ? (e) => onParentHover(parentDocId, e.currentTarget, isRef) : undefined}
                  onMouseLeave={onParentHoverLeave}
                >
                  {parentLabel}
                </span>
              ) : (
                <span
                  className="text-text-muted"
                  onMouseEnter={onParentHover && parentDocId ? (e) => onParentHover(parentDocId, e.currentTarget, isRef) : undefined}
                  onMouseLeave={onParentHoverLeave}
                >
                  {parentLabel}
                </span>
              )}
            </>
          ) : dateStr
        }
        actions={onDelete || onRename ? (
          <RowActions
            primary={(<>
              {onRename && (
                <IconButton size="sm" title={t('rename')} onClick={startRename}>
                  <Pencil size={13} />
                </IconButton>
              )}
              {onDelete && (
                <IconButton
                  size="sm"
                  danger
                  filled={deleteAction.armed}
                  title={t('deleteChat')}
                  onClick={e => {
                    e.stopPropagation();
                    deleteAction.handleClick(() => onDelete(session.session_id));
                  }}
                  onMouseLeave={deleteAction.disarm}
                >
                  <Trash2 size={13} />
                </IconButton>
              )}
            </>)}
          />
        ) : undefined}
      />
    </ListPill>
  );
});
