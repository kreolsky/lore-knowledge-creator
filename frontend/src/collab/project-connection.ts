/** Project-level WS: lifecycle broadcasts + chat frames.
 *  No OT, no content sync — only receives broadcasts from the backend project WS.
 */
// ARCH: Broadcast-only WS — no OT, no content sync: document/reference lifecycle
// broadcasts plus verbatim chat frames (the chat seam's only transport here).
// SYSTEM: project-ws — project-level WebSocket for document/reference lifecycle
// broadcasts and chat frames

import type { WsStatus } from './ws-status';
import { ReconnectController, MAX_RECONNECT_ATTEMPTS } from './reconnect-controller';
import { isAuthCloseCode } from './ws-close-codes';
import { logCollabEvent } from './collab-log';
import { t } from '../i18n';
import type { AgentStep } from '../types';

const HEARTBEAT_INTERVAL_MS = 30_000;

export interface ProjectWsCallbacks {
  onDocumentCreated: (documentId: string, title: string, parentId: string | null, sortKey: string | null) => void;
  onDocumentRenamed: (documentId: string, title: string) => void;
  onDocumentMoved: (
    documentId: string,
    parentId: string | null,
    sortKey: string | null,
    previousParentId: string | null,
    // undefined = the emitter did not state a kind (treat as unchanged — a plain
    // move); true/false = the node's kind AFTER the move (a conversion).
    isReference?: boolean,
    // The node's title — the only source for the tree insert when a reference
    // converts into a tree document on another client.
    title?: string | null,
  ) => void;
  onDocumentReordered: (documentId: string, parentId: string | null, sortKey: string | null) => void;
  onDocumentDeleted: (documentId: string) => void;
  onDocumentsDeletedBatch: (documentIds: string[], referenceIds: string[]) => void;
  // Cross-project subtree move (documents/move.py): OUT on the source project,
  // IN on the target project.
  onDocumentsMovedOut: (
    documentIds: string[],
    referenceIds: string[],
    targetProjectId: string,
    targetProjectName: string,
  ) => void;
  onDocumentsMovedIn: (documentIds: string[]) => void;
  onReferenceCreated: (
    referenceId: string,
    title: string,
    documentId: string | null,
    createdBy: string | null,
    createdByName: string | null,
  ) => void;
  onReferenceRenamed: (referenceId: string, title: string) => void;
  onReferenceMoved: (referenceId: string, documentId: string | null) => void;
  onReferenceDeleted: (referenceId: string) => void;
  onReferenceUpdated: (referenceId: string) => void;
  onReferenceStatusChanged: (referenceId: string, status: string) => void;
  onContentFlushed: (entityId: string, entityType: string) => void;
  onAgentExtractionStarted: (referenceId: string) => void;
  onGenerateImageProgress: (sessionId: string, phase: string, runId: string, messageId: string) => void;
  onGenerateImageDone: (event: {
    sessionId: string;
    runId: string;
    messageId: string;
    referenceIds: string[];
    title: string;
    refine: { prompt: string; ok: boolean; error: string | null };
    steps: AgentStep[];
  }) => void;
  onGenerateImageFailed: (event: {
    sessionId: string;
    runId: string;
    messageId: string;
    error: string;
  }) => void;
  onExtractionError: (referenceId: string, noteId: string, documentId: string) => void;
  onAgentErrorNote: (noteId: string, documentId: string) => void;
  // One chat_frame envelope from the owner-filtered fan-out (SYSTEM:
  // chat-fanout) — the frame is VERBATIM, and the chat store's dispatch owns
  // what it does with it (a session with no open turn is another tab's
  // business, not this connection's).
  onChatFrame: (sessionId: string, frame: Record<string, unknown>) => void;
  // Fired when a socket OPENS after
  // a drop (attempts > 0 at open). Frames pushed into the outage are gone for
  // this client (the fan-out sends to live sockets only) — the consumer
  // reloads what it must (the store's open-harness-turn resync).
  onProjectWsResync: () => void;
  onEmbeddingDegraded: () => void;
  onEmbeddingRecovered: () => void;
  onProjectUpdated: (updates: Record<string, unknown>) => void;
  onProjectDeleted: () => void;
  onStatusChange: (status: WsStatus) => void;
  onError: (message: string) => void;
}

function isString(v: unknown): v is string { return typeof v === 'string'; }

export class ProjectConnection {
  private ws: WebSocket | null = null;
  private projectId: string;
  private callbacks: ProjectWsCallbacks;
  private reconnectController = new ReconnectController();
  private _status: WsStatus = 'connecting';
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private _everConnected = false;
  private _lastCloseTs: number | null = null;

  constructor(projectId: string, callbacks: ProjectWsCallbacks) {
    this.projectId = projectId;
    this.callbacks = callbacks;
  }

  get status(): WsStatus {
    return this._status;
  }

  connect() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    this.setStatus(this._status === 'connected' ? 'reconnecting' : 'connecting');
    logCollabEvent('project', 'connect', undefined, {
      initial: !this._everConnected,
      attempt: this.reconnectController.attempts,
    });

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws/project/${this.projectId}`);

    this.ws.onopen = () => {
      // Read BEFORE resetBackoff zeroes it: attempts > 0 means this open
      // followed a drop — the resync signal (frames were lost in the gap).
      const reopenedAfterDrop = this.reconnectController.attempts > 0;
      this.reconnectController.resetBackoff();
      logCollabEvent('project', 'open', undefined, {
        outageMs: this._lastCloseTs !== null ? Date.now() - this._lastCloseTs : undefined,
      });
      this._everConnected = true;
      this._startHeartbeat();
      if (reopenedAfterDrop) this.callbacks.onProjectWsResync();
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.dispatch(msg);
      } catch (e) {
        console.warn('[project-ws] malformed WS message', e);
        this.callbacks.onError('Malformed server message');
      }
    };

    this.ws.onclose = (event: CloseEvent) => {
      this._clearHeartbeat();
      this._lastCloseTs = Date.now();
      logCollabEvent('project', 'close', undefined, {
        code: event.code,
        reason: event.reason,
        wasClean: event.wasClean,
        intentional: this.reconnectController.intentional,
        isAuth: isAuthCloseCode(event.code),
      });
      if (this.reconnectController.intentional) return;
      // ARCH: auth errors are permanent (4001=Unauthorized, 4003=No access, 4004=Not
      // found) — no reconnect can fix them. Previously project-ws reconnected up to
      // MAX_RECONNECT_ATTEMPTS (~10+ min) after a 4003 kick, wasteful and confusing
      // (reconnecting toast after being kicked). Mirrors yjs-provider's policy via the
      // shared AUTH_CLOSE_CODES constant so the two clients never drift.
      if (isAuthCloseCode(event.code)) {
        this.setStatus('offline');
        this.callbacks.onError(t('accessRevokedOrExpired'));
        return;
      }
      this.setStatus('reconnecting');
      const result = this.reconnectController.schedule(() => this.connect());
      if (result === 'gave_up') {
        console.warn(`[project-ws] giving up after ${MAX_RECONNECT_ATTEMPTS} attempts`);
        logCollabEvent('project', 'gave-up', undefined, { attempt: this.reconnectController.attempts });
        this.setStatus('offline');
      } else {
        logCollabEvent('project', 'reconnect-scheduled', undefined, {
          attempt: this.reconnectController.attempts,
          delay: this.reconnectController.lastScheduledDelay,
        });
      }
    };

    this.ws.onerror = (event) => {
      console.warn('[project-ws] error', event);
    };
  }

  disconnect() {
    this._clearHeartbeat();
    this.reconnectController.markIntentional();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  private setStatus(s: WsStatus) {
    this._status = s;
    this.callbacks.onStatusChange(s);
  }

  private dispatch(msg: Record<string, unknown>) {
    switch (msg.type) {
      case 'init':
        this.setStatus('connected');
        break;
      case 'document_created':
        if (!isString(msg.document_id) || !isString(msg.title)) return;
        this.callbacks.onDocumentCreated(
          msg.document_id,
          msg.title,
          isString(msg.parent_id) ? msg.parent_id : null,
          isString(msg.sort_key) ? msg.sort_key : null,
        );
        break;
      case 'document_renamed':
        if (!isString(msg.document_id) || !isString(msg.title)) return;
        this.callbacks.onDocumentRenamed(msg.document_id, msg.title);
        break;
      case 'document_moved':
        if (!isString(msg.document_id)) return;
        this.callbacks.onDocumentMoved(
          msg.document_id,
          isString(msg.parent_id) ? msg.parent_id : null,
          isString(msg.sort_key) ? msg.sort_key : null,
          // The PREVIOUS host, so a client showing either host can refresh its
          // per-host reference panel (the move changes WHERE a reference is listed).
          isString(msg.previous_parent_id) ? msg.previous_parent_id : null,
          // The kind AFTER the move: present only on emitters that state it
          // (the agent move executor); a human-UI move omits it → undefined =
          // kind unchanged. Strict boolean check — 1/"true" must not pass.
          typeof msg.is_reference === 'boolean' ? msg.is_reference : undefined,
          isString(msg.title) ? msg.title : null,
        );
        break;
      case 'document_reordered':
        if (!isString(msg.document_id)) return;
        this.callbacks.onDocumentReordered(
          msg.document_id,
          isString(msg.parent_id) ? msg.parent_id : null,
          isString(msg.sort_key) ? msg.sort_key : null,
        );
        break;
      case 'document_deleted':
        if (!isString(msg.document_id)) return;
        this.callbacks.onDocumentDeleted(msg.document_id);
        break;
      case 'documents_deleted_batch':
        if (!Array.isArray(msg.document_ids)) return;
        this.callbacks.onDocumentsDeletedBatch(
          msg.document_ids as string[],
          Array.isArray(msg.reference_ids) ? msg.reference_ids as string[] : [],
        );
        break;
      case 'documents_moved_out':
        if (!Array.isArray(msg.document_ids)) return;
        this.callbacks.onDocumentsMovedOut(
          msg.document_ids as string[],
          Array.isArray(msg.reference_ids) ? msg.reference_ids as string[] : [],
          isString(msg.target_project_id) ? msg.target_project_id : '',
          isString(msg.target_project_name) ? msg.target_project_name : '',
        );
        break;
      case 'documents_moved_in':
        if (!Array.isArray(msg.document_ids)) return;
        this.callbacks.onDocumentsMovedIn(msg.document_ids as string[]);
        break;
      case 'reference_created':
        if (!isString(msg.reference_id) || !isString(msg.title)) return;
        this.callbacks.onReferenceCreated(
          msg.reference_id,
          msg.title,
          isString(msg.document_id) ? msg.document_id : null,
          isString(msg.created_by) ? msg.created_by : null,
          isString(msg.created_by_name) ? msg.created_by_name : null,
        );
        break;
      case 'reference_renamed':
        if (!isString(msg.reference_id) || !isString(msg.title)) return;
        this.callbacks.onReferenceRenamed(msg.reference_id, msg.title);
        break;
      case 'reference_moved':
        if (!isString(msg.reference_id)) return;
        this.callbacks.onReferenceMoved(
          msg.reference_id,
          isString(msg.document_id) ? msg.document_id : null,
        );
        break;
      case 'reference_deleted':
        if (!isString(msg.reference_id)) return;
        this.callbacks.onReferenceDeleted(msg.reference_id);
        break;
      case 'reference_updated':
        if (!isString(msg.reference_id)) return;
        this.callbacks.onReferenceUpdated(msg.reference_id);
        break;
      case 'reference_status_changed':
        if (!isString(msg.reference_id) || !isString(msg.status)) return;
        this.callbacks.onReferenceStatusChanged(msg.reference_id, msg.status);
        break;
      case 'content_flushed':
        if (!isString(msg.entity_id) || !isString(msg.entity_type)) return;
        this.callbacks.onContentFlushed(msg.entity_id, msg.entity_type);
        break;
      case 'agent_extraction_started':
        if (!isString(msg.reference_id)) return;
        this.callbacks.onAgentExtractionStarted(msg.reference_id);
        break;
      case 'generate_image_progress':
        if (!isString(msg.session_id) || !isString(msg.phase)) return;
        this.callbacks.onGenerateImageProgress(
          msg.session_id, msg.phase,
          isString(msg.run_id) ? msg.run_id : '',
          isString(msg.message_id) ? msg.message_id : '',
        );
        break;
      case 'generate_image_done': {
        if (!isString(msg.session_id) || !isString(msg.run_id)) return;
        const refineRaw = msg.refine as Record<string, unknown> | null;
        const refine = (refineRaw && typeof refineRaw === 'object')
          ? {
              prompt: isString(refineRaw.prompt) ? refineRaw.prompt : '',
              ok: refineRaw.ok === true,
              error: isString(refineRaw.error) ? refineRaw.error : null,
            }
          : { prompt: '', ok: true, error: null };
        const rawSteps = Array.isArray(msg.steps) ? msg.steps : [];
        // Validate + narrow each step to AgentStep; drop malformed entries so an
        // untrusted WS frame cannot inject arbitrary keys.
        const steps: AgentStep[] = rawSteps
          .map((s): AgentStep | null => {
            if (!s || typeof s !== 'object') return null;
            const r = s as Record<string, unknown>;
            if (!isString(r.tool_call_id) || !isString(r.tool)) return null;
            const out: AgentStep = {
              tool_call_id: r.tool_call_id,
              tool: r.tool,
              summary: isString(r.summary) ? r.summary : '',
            };
            if (isString(r.detail)) out.detail = r.detail;
            if (isString(r.outcome)) out.outcome = r.outcome as AgentStep['outcome'];
            if (Array.isArray(r.image_ref_ids)) {
              out.image_ref_ids = r.image_ref_ids.filter((x): x is string => typeof x === 'string');
            }
            if (isString(r.run_id)) out.run_id = r.run_id;
            return out;
          })
          .filter((s): s is AgentStep => s !== null);
        this.callbacks.onGenerateImageDone({
          sessionId: msg.session_id,
          runId: msg.run_id,
          messageId: isString(msg.message_id) ? msg.message_id : '',
          referenceIds: Array.isArray(msg.reference_ids)
            ? msg.reference_ids.filter((r: unknown): r is string => typeof r === 'string')
            : [],
          title: isString(msg.title) ? msg.title : '',
          refine,
          steps,
        });
        break;
      }
      case 'generate_image_failed':
        if (!isString(msg.session_id) || !isString(msg.run_id)) return;
        this.callbacks.onGenerateImageFailed({
          sessionId: msg.session_id,
          runId: msg.run_id,
          messageId: isString(msg.message_id) ? msg.message_id : '',
          error: isString(msg.error) ? msg.error : '',
        });
        break;
      case 'extraction_error':
        if (!isString(msg.reference_id) || !isString(msg.note_id) || !isString(msg.document_id)) return;
        this.callbacks.onExtractionError(msg.reference_id, msg.note_id, msg.document_id);
        break;
      case 'agent_error_note':
        if (!isString(msg.note_id) || !isString(msg.document_id)) return;
        this.callbacks.onAgentErrorNote(msg.note_id, msg.document_id);
        break;
      case 'chat_frame': {
        // Beside the image chips (the fan-out's own event table row): the
        // harness lifecycle's chat frames ride this channel owner-filtered.
        const frame = msg.frame;
        if (!isString(msg.session_id) || !frame || typeof frame !== 'object') return;
        this.callbacks.onChatFrame(msg.session_id, frame as Record<string, unknown>);
        break;
      }
      case 'embedding_degraded':
        this.callbacks.onEmbeddingDegraded();
        break;
      case 'embedding_recovered':
        this.callbacks.onEmbeddingRecovered();
        break;
      case 'project_updated':
        this.callbacks.onProjectUpdated(msg);
        break;
      case 'project_deleted':
        this.callbacks.onProjectDeleted();
        break;
    }
  }

  private _clearHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private _startHeartbeat() {
    this._clearHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'heartbeat' }));
      }
    }, HEARTBEAT_INTERVAL_MS);
  }

}
