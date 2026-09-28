/**
 * Doc-layer collab callbacks + control-frame dispatch.
 *
 * The doc-channel half of the JSON control-frame surface: document lifecycle
 * (deleted/backlinks/checkpoints/history), save-degradation signaling, agent
 * presence and note-session realtime — frames that only exist because the
 * entity is a document. The provider imports this module for its callback
 * surface; the entity-generic frames live in yjs-events.ts.
 */

import { isObject, isString, broadcastToBundles, type YjsCollabCallbacks } from './yjs-events';

/** Full callback surface for a doc-channel collab consumer (extends the generic set). */
export interface DocCollabCallbacks extends YjsCollabCallbacks {
  onDocDeleted: () => void;
  onBacklinksChanged: () => void;
  onCheckpointCreated: (checkpoint: Record<string, unknown>) => void;
  onDocumentHistoryAdded: (entry: Record<string, unknown>) => void;
  onSaveDegraded: () => void;
  onSaveRecovered: () => void;
  // Optional: the home agent just edited this document.
  // Decorative — undefined when the consumer does not render agent presence.
  onAgentEditing?: (onBehalfOf: string | null) => void;
  // SYSTEM: note-realtime — note-session lifecycle
  // + message-change frames broadcast on the doc collab session. Optional: consumers
  // that do not render notes leave these undefined (default no-op).
  onNoteSessionCreated?: (session: Record<string, unknown>) => void;
  onNoteSessionDeleted?: (sessionId: string) => void;
  onNoteMessageChanged?: (payload: Record<string, unknown>) => void;
}

/**
 * Dispatch a doc-layer control frame to every bundle.
 * Returns true when the frame type was consumed (even if its payload guard
 * dropped it or there is no entity); an unhandled type returns false.
 */
export function dispatchDocControlFrame(
  msg: Record<string, unknown>,
  bundles: DocCollabCallbacks[] | null,
): boolean {
  switch (msg.type) {
    case 'doc_deleted':
      if (bundles) broadcastToBundles(bundles, cb => cb.onDocDeleted());
      return true;

    case 'backlinks_changed':
      if (bundles) broadcastToBundles(bundles, cb => cb.onBacklinksChanged());
      return true;

    case 'checkpoint_created':
      if (bundles && isObject(msg.checkpoint)) {
        const cp = msg.checkpoint;
        broadcastToBundles(bundles, cb => cb.onCheckpointCreated(cp));
      }
      return true;

    case 'document_history_added':
      if (bundles && isObject(msg.event)) {
        const entry = msg.event;
        broadcastToBundles(bundles, cb => cb.onDocumentHistoryAdded(entry));
      }
      return true;

    case 'save_degraded':
      if (bundles) broadcastToBundles(bundles, cb => cb.onSaveDegraded());
      return true;

    case 'save_recovered':
      if (bundles) broadcastToBundles(bundles, cb => cb.onSaveRecovered());
      return true;

    // Three note-specific (see SYSTEM: note-realtime)
    // frame types. The payload is forwarded verbatim; useCollabConnection translates
    // them to the app EventBus events (collab-note-created/updated/deleted) consumed
    // by useNoteCrud. Separate names (not a unified chat_session_changed) because
    // notes and AI-chat carry different payloads (extension point for Layer C).
    case 'note_session_created': {
      // Accept both a full session (chat routes) and a minimal {session_id}
      // frame (system-note path: extractor/agent error notes carry no `session`).
      // The minimal frame drives a best-effort loadSessions on the consumer
      // (useNoteCrud isFullSession=false) — without this fallback the frame is
      // silently dropped and system notes never refresh live.
      const session = isObject(msg.session) ? msg.session : { session_id: msg.session_id };
      if (bundles && isObject(session) && isString((session as Record<string, unknown>).session_id)) {
        broadcastToBundles(bundles, cb => cb.onNoteSessionCreated?.(session as Record<string, unknown>));
      }
      return true;
    }

    case 'note_session_deleted':
      if (bundles && isString(msg.session_id)) {
        const sessionId = msg.session_id;
        broadcastToBundles(bundles, cb => cb.onNoteSessionDeleted?.(sessionId));
      }
      return true;

    case 'note_message_changed':
      if (bundles && isObject(msg)) {
        const payload = msg;
        broadcastToBundles(bundles, cb => cb.onNoteMessageChanged?.(payload));
      }
      return true;

    case 'agent_editing':
      // Decorative presence: the agent edited this doc on behalf of <user>
      // Only consumers that render agent presence subscribe.
      if (bundles) {
        const onBehalfOf = isString(msg.on_behalf_of) ? msg.on_behalf_of : null;
        broadcastToBundles(bundles, cb => cb.onAgentEditing?.(onBehalfOf));
      }
      return true;

    default:
      return false;
  }
}
