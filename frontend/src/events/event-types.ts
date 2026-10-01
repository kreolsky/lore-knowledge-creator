/** Central registry of all application events and their payload types. */
// ARCH: Events include editorView when originating from CM6 — multi-editor awareness (main + snapshot/cell editors).

import type { EditorView } from '@codemirror/view';
import type { Checkpoint, DocumentHistoryEntry } from '../types';

// ARCH: the project WS forwards an event's emitted kwargs verbatim (minus
// project_id); the field names ARE the contract, typed once below under
// 'ws:<type>' — the wire names (snake_case) stay snake_case, never renamed.
// tests/backend/test_event_wiring.py binds these types to the backend emit
// sites: a field added there and not typed here fails CI. The wire types this
// connection can deliver besides 'init' and 'chat_frame' (which dispatch
// handles as its own arms) — see SYSTEM: project-ws.
export const WS_EVENT_TYPES = [
  'document_created',
  'document_renamed',
  'document_moved',
  'document_reordered',
  'document_deleted',
  'documents_deleted_batch',
  'documents_moved_out',
  'documents_moved_in',
  'reference_created',
  'reference_renamed',
  'reference_moved',
  'reference_deleted',
  'reference_updated',
  'reference_status_changed',
  'content_flushed',
  'agent_extraction_started',
  'extraction_error',
  'agent_error_note',
  'embedding_degraded',
  'embedding_recovered',
  'project_updated',
  'project_deleted',
] as const;

export type WsEventKey = `ws:${(typeof WS_EVENT_TYPES)[number]}`;

export interface EventMap {
  // ── Navigation ──
  // restore = "reopen the document as it was" — its remembered last-opened
  // reference. The tree is the ONLY restoring source. Default (omitted) = open
  // the document BODY — app-store.focusDocument owns that decision.
  'navigate-to-document': { documentId: string; restore?: true };
  // sourceDocId: null = the jump has NO return point (goto-parent from the panel /
  // chat plaque) — the tree must not keep the doc we left highlighted as the
  // reference's source. Omitted = today's behaviour (source = the doc we navigate from).
  'navigate-to-reference': { referenceId: string; stayInContext?: boolean; sourceDocId?: string | null; scrollToOffset?: number };
  'scroll-to-line': { line: number };
  'scroll-to-note-in-editor': { noteId: string };

  // ── WS bridge: project tree (wire field names, verbatim) ──
  'ws:document_created': { document_id: string; title: string; parent_id: string | null; sort_key: string | null; is_reference?: boolean };
  'ws:document_renamed': { document_id: string; title: string };
  // previous_parent_id lets the OLD host recognize the move concerns it;
  // is_reference (the node's FINAL kind) + title let a second client move the
  // node between the tree and a references panel on a conversion.
  'ws:document_moved': {
    document_id: string;
    parent_id: string | null;
    sort_key: string | null;
    previous_parent_id: string | null;
    is_reference: boolean;
    title: string | null;
  };
  'ws:document_reordered': { document_id: string; parent_id: string | null; sort_key: string | null };
  'ws:document_deleted': { document_id: string };
  'ws:documents_deleted_batch': { document_ids: string[]; reference_ids: string[] };
  // Cross-project subtree move (SYSTEM: project-ws). OUT rides the SOURCE
  // project's channel: tree + refs drop their ids; a client whose open doc is
  // in the set toasts the target project and follows via a hard /docs/<id>
  // reload (the store, both WS connections and the collab join set are all
  // keyed by the old project — piecemeal reset is the bug surface). IN rides
  // the TARGET's channel and means "refetch the whole tree" (the subtree may
  // be large and the tree needs full rows).
  'ws:documents_moved_out': {
    document_ids: string[];
    reference_ids: string[];
    target_project_id: string;
    target_project_name: string;
  };
  'ws:documents_moved_in': { document_ids: string[] };
  'ws:reference_created': {
    reference_id: string;
    title: string;
    document_id: string | null;
    created_by: string | null;
    created_by_name: string | null;
  };
  'ws:reference_renamed': { reference_id: string; title: string };
  // sort_key: the ref's key in its (new) host's group — the handler re-places
  // the ref in that group's run instead of splitting the old one.
  'ws:reference_moved': { reference_id: string; document_id: string | null; sort_key: string | null };
  'ws:reference_deleted': { reference_id: string };
  'ws:reference_updated': { reference_id: string };
  'ws:reference_status_changed': { reference_id: string; status: string };
  // see SYSTEM: transclusion — a doc's content settled after a debounce flush. High-frequency
  // (every flush, every doc); listeners MUST gate on an existing doc transcludeMap entry.
  // is_reference rides the payload only when the emitter states it.
  'ws:content_flushed': { entity_id: string; entity_type: string; is_reference?: boolean };
  'ws:agent_extraction_started': { reference_id: string };
  // The detached image generation's live facts do NOT ride the project WS:
  // they are chat facts — backend-minted `lore/image-gen` frames on the
  // owner-filtered chat channel (chat_frame envelopes) — so no
  // ws:generate_image_* types exist.
  'ws:extraction_error': { reference_id: string; note_id: string; document_id: string };
  'ws:agent_error_note': { note_id: string; document_id: string };
  'ws:embedding_degraded': Record<string, never>;
  'ws:embedding_recovered': Record<string, never>;
  'ws:project_updated': { updates: Record<string, unknown> };
  'ws:project_deleted': Record<string, never>;

  // ── WS bridge: collab ──
  'collab-backlinks-changed': void;
  'snapshot-created': Checkpoint;
  'document-history-added': DocumentHistoryEntry;
  // SYSTEM: note-realtime — WS frame → app EventBus.
  // 'created' carries the full serialized note session (or a minimal {session_id}
  // frame from the system-note path); 'updated' carries {action, session_id, ...};
  // 'deleted' carries {sessionId}.
  'collab-note-created': { session_id: string; [key: string]: unknown };
  'collab-note-updated': { action: string; session_id: string; [key: string]: unknown };
  'collab-note-deleted': { sessionId: string };

  // ── UI panels ──
  'open-right-panel': void;
  // ARCH: carries the live editor selection so FindReplacePanel can seed the
  // search field on every Cmd+F (fresh open AND repeat) — see FindReplacePanel.
  'open-find': { selection: string };
  'open-notes': { threadId?: string };
  'highlight-note': { noteId: string };
  'connect-note': { noteId: string };
  'create-note-from-editor': { noteId: string };

  // ── Editor ──
  'show-link-suggestions': { pos: number; coords: { top: number; lineTop: number; left: number } | null; editorView: EditorView };
  'editor-selection-change': { empty: boolean };
  'editor-doc-changed': void;
  // see SYSTEM: selection-region-agent — the user clicked the Bot icon on the selection
  // toolbar. Carries the pinned region: doc_id, a Yjs RelativePosition JSON pair
  // (survives edits), and the live code-point offsets + text snapshot. The chat
  // layer listens and starts a pinned agent session.
  'start-agent-chat': {
    doc_id: string;
    relFrom: unknown;
    relTo: unknown;
    from_cp: number;
    to_cp: number;
    text: string;
  };

  // ── Sidebar ──
  'open-sidebar-docs': void;
  // Reveal a document in the tree: expand its ancestors, switch the sidebar to
  // the docs tab, and center the row. Explicit gesture only — never auto
  // (auto-following would fight a tree the user collapsed by hand). Fired by the
  // breadcrumb's active crumb.
  'reveal-in-tree': { documentId: string };
  'open-create-doc': { parentId?: string };
  'document-deleted': { documentId: string };
  'breadcrumb-start-rename': void;

  // ── Recording ──
  'start-recording': void;
  'stop-recording': void;
  'voice-transcription-complete': { text: string; referenceId: string; editorView: EditorView };
  'voice-transcription-error': { editorView: EditorView };

  // ── Reference banner ──
  'reset-reference-banner': void;

  // ── Image resolution ──
  'unresolved-image-ref': void;

  // ── Chat clarify ──
  // Fired by ChatClarifyPopover when the user confirms a clarifying question on a
  // selected fragment of an assistant message. ChatInput appends it to the draft
  // as a markdown blockquote + question.
  'chat-clarify-insert': { quote: string; question: string };
}
