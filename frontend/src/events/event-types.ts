/** Central registry of all application events and their payload types. */
// ARCH: Events include editorView when originating from CM6 — multi-editor awareness (main + snapshot/cell editors).

import type { EditorView } from '@codemirror/view';
import type { AgentStep, Checkpoint, DocumentHistoryEntry } from '../types';

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

  // ── WS bridge: project tree ──
  'project-document-created': { documentId: string; title: string; parentId: string | null; sortKey: string | null };
  'project-document-renamed': { documentId: string; title: string };
  'project-document-moved': {
    documentId: string;
    parentId: string | null;
    sortKey: string | null;
    previousParentId: string | null;
    // undefined = kind not stated by the emitter (a plain move); true/false =
    // the node's kind AFTER the move — set only when the kind CHANGED (a
    // move_document conversion), so the tree can drop/insert the node.
    isReference?: boolean;
    // Title for the tree insert on a reference→document conversion.
    title?: string | null;
  };
  'project-document-reordered': { documentId: string; parentId: string | null; sortKey: string | null };
  'project-document-deleted': { documentId: string };
  'project-documents-deleted-batch': { documentIds: string[]; referenceIds: string[] };
  // Cross-project subtree move (SYSTEM: project-ws). OUT rides the SOURCE
  // project's channel: tree + refs drop their ids; a client whose open doc is
  // in the set toasts the target project and follows via a hard /docs/<id>
  // reload (the store, both WS connections and the collab join set are all
  // keyed by the old project — piecemeal reset is the bug surface). IN rides
  // the TARGET's channel and means "refetch the whole tree" (the subtree may
  // be large and the tree needs full rows).
  'project-documents-moved-out': {
    documentIds: string[];
    referenceIds: string[];
    targetProjectId: string;
    targetProjectName: string;
  };
  'project-documents-moved-in': { documentIds: string[] };
  'project-reference-created': {
    referenceId: string;
    title: string;
    documentId: string | null;
    createdBy: string | null;
    createdByName: string | null;
  };
  'project-reference-renamed': { referenceId: string; title: string };
  'project-reference-moved': { referenceId: string; documentId: string | null };
  'project-reference-deleted': { referenceId: string };
  'project-reference-updated': { referenceId: string };
  'project-reference-status-changed': { referenceId: string; status: string };
  // see SYSTEM: transclusion — a doc's content settled after a debounce flush. High-frequency
  // (every flush, every doc); listeners MUST gate on an existing doc transcludeMap entry.
  'project-content-flushed': { entityId: string; entityType: string };
  'project-agent-extraction-started': { referenceId: string };
  // Live generate_image phase (project-WS sourced).
  // Carries messageId so the spinner can be pinned to
  // the generating message after the turn ends (the phase outlives the turn).
  'project-generate-image-progress': { sessionId: string; phase: string; runId: string; messageId: string };
  // Generation is detached — the tool
  // returns {status:'generating'} at once and the image lands asynchronously.
  // `done` carries the refiner outcome + image reference ids + the ALREADY-BUILT
  // step dicts (single source — the frontend stamps them verbatim, no
  // reconstruction drift) correlated by sessionId + runId; `failed` carries the
  // cause (no-silent-degradation). The chips are persisted server-side; these
  // drive the LIVE update for the active session and a non-active session shows
  // them on return/reload.
  'project-generate-image-done': {
    sessionId: string;
    runId: string;
    messageId: string;
    referenceIds: string[];
    title: string;
    refine: { prompt: string; ok: boolean; error: string | null };
    steps: AgentStep[];
  };
  'project-generate-image-failed': {
    sessionId: string;
    runId: string;
    messageId: string;
    error: string;
  };
  'project-extraction-error': { referenceId: string; noteId: string; documentId: string };
  'project-agent-error-note': { noteId: string; documentId: string };
  'project-updated': Record<string, unknown>;
  'project-deleted': void;

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
