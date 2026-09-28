/**
 * Manages the collab connection lifecycle for a document or reference.
 *
 * ARCH: Uses YjsProjectProvider (shared per-project WS) via context.
 * Yjs handles sync internally — no OT, no pending ops, no version tracking.
 * Returns { handleRef, connEpoch }: the EntityYjsState ref for the editor to
 * wire yCollab, plus an epoch bumped on every handle assignment/clear so
 * consumers re-run when the handle lands after their first render.
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useProjectCollab } from '../collab/ProjectCollabContext';
import type { EntityYjsState } from '../collab/yjs-provider';
import { DOC } from '../collab/entity-types';
import { createYjsExtension } from '../collab/yjs-binding';
import { AUTO_BACKUP_TOAST_KEYS } from '../collab/backup-labels';
import { clearCheckpointDedup } from '../editor/content-sync';
import { clearLinkCache } from '../api/links';
import { useAppStore } from '../store/app-store';
import { useDocumentRoute } from './useDocumentRoute';
import { userColor } from '../utils/user-color';
import { emit } from '../events';
import { t } from '../i18n';
import type { AccessLevel, Checkpoint, PresenceUser } from '../types';
import type { WsStatus } from '../collab/ws-status';

const VALID_ACCESS_LEVELS: readonly string[] = ['full', 'commentator', 'readonly'];

function isAccessLevel(v: string): v is AccessLevel {
  return VALID_ACCESS_LEVELS.includes(v);
}

function isCheckpointPayload(v: unknown): v is Checkpoint {
  if (v === null || typeof v !== 'object') return false;
  const o = v as Record<string, unknown>;
  return typeof o.checkpoint_id === 'string' && typeof o.content === 'string';
}

// The toast allowlist (AUTO_BACKUP_TOAST_KEYS) is imported from collab/backup-labels.

/** Local user for awareness — id/name from the auth store, color via userColor. */
function localPresenceUser() {
  const user = useAppStore.getState().currentUser;
  const id = user?.user_id ?? '';
  return { id, name: user?.name ?? '', color: userColor(id) };
}

// WHY: the server presence payload carries access_level so the client can filter to
// editors for the "editing now" header. It is optional on the wire (older payloads omit
// it); a missing level must NOT count as an editor, so default to 'readonly' (the most
// restrictive non-editing level). Mirrors the server _has_pushed discriminator.
function normalizePresenceUser(u: { user_id: string; name: string; access_level?: AccessLevel }): PresenceUser {
  return { user_id: u.user_id, name: u.name, access_level: u.access_level ?? 'readonly' };
}
function normalizePresence(users: { user_id: string; name: string; access_level?: AccessLevel }[]): PresenceUser[] {
  return users.map(normalizePresenceUser);
}

interface UseCollabConnectionOpts {
  /** Active entity id (deferred to match the live editor view). */
  entityId: string | null | undefined;
  /** True when entityId refers to a reference; false for a document. */
  isReference: boolean;
  /** Called after collab init completes. */
  onCollabInitDone?: (textChanged: boolean) => void;
  /** Tracks WS lifecycle. */
  onStatusChange?: (status: WsStatus) => void;
}

/**
 * Join the collab session for the given entity via the shared project WS.
 * Returns a ref to the active EntityYjsState (or null).
 */
export function useCollabConnection({
  entityId,
  isReference,
  onCollabInitDone,
  onStatusChange,
}: UseCollabConnectionOpts) {
  const previewDocument = useAppStore(s => s.previewDocument);
  const navigate = useNavigate();
  const { projectId } = useDocumentRoute();
  const projectCollab = useProjectCollab();
  const handleRef = useRef<EntityYjsState | null>(null);
  // INVARIANT: connEpoch bumps at EVERY handleRef.current assignment/clear. Why:
  // the ref mutation itself does not re-render, so consumers that must react to
  // the handle LANDING (useEditorCollab's publish effect) never re-fire when the
  // connection arrives after their first run (projectCollab landing post-mount) —
  // the publish ran once against a null connection and the slot stayed empty.
  const [connEpoch, setConnEpoch] = useState(0);
  const previewRef = useRef(previewDocument);
  const warnedDegradedRef = useRef<string | null>(null);
  previewRef.current = previewDocument;
  // The entity identity at callback time: the opts bag handed to joinEntity is a
  // stable closure, so a sync arriving from a PREVIOUS join must be attributable.
  const entityIdRef = useRef(entityId);
  entityIdRef.current = entityId;

  useEffect(() => {
    if (!projectCollab) return;

    // Presence resets on every entity switch — no leftover chips from the prior doc.
    useAppStore.getState().setCollabUsers([]);

    if (previewRef.current) {
      if (handleRef.current) {
        projectCollab.leaveEntity(entityId!);
      }
      handleRef.current = null;
      setConnEpoch(e => e + 1);
      return;
    }

    const entityType = DOC;
    if (!entityId) {
      handleRef.current = null;
      setConnEpoch(e => e + 1);
      return;
    }

    const reattaching = projectCollab.hasEntity(entityId) && projectCollab.status === 'connected';
    // Captured per join: this effect's entity. Callbacks below compare against
    // entityIdRef to detect a sync that belongs to a join that is no longer current.
    const joinEntityId = entityId;

    const entityState = projectCollab.joinEntity(entityType, entityId, {
      onPresenceUsers: (users) => useAppStore.getState().setCollabUsers(normalizePresence(users)),
      onUserJoined: (user) => useAppStore.getState().addCollabUser(normalizePresenceUser(user)),
      onUserLeft: (userId) => useAppStore.getState().removeCollabUser(userId),
      onDocDeleted: () => {
        clearCheckpointDedup(entityId);
        if (isReference) {
          useAppStore.getState().setCurrentReference(null);
        } else if (projectId) {
          navigate(`/projects/${projectId}`);
        }
      },
      onAccessChanged: (level) => {
        if (!isAccessLevel(level)) return;
        useAppStore.getState().setAccessLevel(level);
      },
      onAccessRevoked: () => {
        clearCheckpointDedup(entityId);
        if (isReference) {
          useAppStore.getState().setCurrentReference(null);
        } else if (projectId) {
          navigate(`/projects/${projectId}`);
        }
      },
      onStatusChange: (status) => {
        onStatusChange?.(status);
      },
      onSynced: () => {
        // INVARIANT(corruption): a sync completion from a previous entity's join
        // (fired while its leave still pends behind the checkpoint) must not
        // report init for the current entity.  Why: the opts closure is per-join; without the identity check a late sync of the OLD document flips readiness/editor live against the NEW entity's unsynced CRDT state — the historical stale-init race.
        if (joinEntityId !== entityIdRef.current) return;
        onStatusChange?.('connected');
        onCollabInitDone?.(false);
        const doc = useAppStore.getState().currentDocument;
        if (doc?.last_save_failed_at && warnedDegradedRef.current !== doc.document_id) {
          warnedDegradedRef.current = doc.document_id;
          useAppStore.getState().showToast(t('collabSaveDegraded'), 'warning');
        }
      },
      onError: (msg) => useAppStore.getState().showToast(msg, 'error'),
      onBacklinksChanged: () => {
        // Restore / external link mutation: drop the warm first-circle cache so the
        // Link2 picker indicator stops showing a stale hint (the event carries no id).
        clearLinkCache();
        emit('collab-backlinks-changed');
      },
      onCheckpointCreated: (checkpoint) => {
        if (!isCheckpointPayload(checkpoint)) return;
        emit('snapshot-created', checkpoint);
        const toastKey = AUTO_BACKUP_TOAST_KEYS[checkpoint.label];
        if (toastKey) useAppStore.getState().showToast(t(toastKey), 'info');
      },
      onDocumentHistoryAdded: (entry) => {
        emit('document-history-added', entry as unknown as import('../types').DocumentHistoryEntry);
      },
      onSaveDegraded: () => {
        useAppStore.getState().showToast(t('collabSaveDegraded'), 'warning');
      },
      onSaveRecovered: () => {
        useAppStore.getState().showToast(t('collabSaveRecovered'), 'info');
      },
      onAgentEditing: (_onBehalfOf) => {
        // Decorative presence: the agent edited the open document.
        // Surfaced as a toast so the edit is never silent; the CRDT converge + the
        // History panel undo remain the real safety net.
        useAppStore.getState().showToast(t('agentEditedDocument'), 'info');
      },
      // WS frame → app (see SYSTEM: note-realtime)
      // EventBus event. These exact event names are consumed by useNoteCrud (and
      // advertised in NotesPanel.tsx's docstring). The payload is forwarded as-is.
      onNoteSessionCreated: (session) => {
        emit('collab-note-created', session as unknown as { session_id: string });
      },
      onNoteSessionDeleted: (sessionId) => {
        emit('collab-note-deleted', { sessionId });
      },
      onNoteMessageChanged: (payload) => {
        emit('collab-note-updated', payload as unknown as { action: string; session_id: string });
      },
    }, localPresenceUser());

    handleRef.current = entityState;
    setConnEpoch(e => e + 1);

    if (reattaching) {
      onStatusChange?.('connected');
      onCollabInitDone?.(false);
    }

    return () => {};
  }, [entityId, isReference, projectCollab, previewDocument]);

  return { handleRef, connEpoch };
}

export { createYjsExtension };
