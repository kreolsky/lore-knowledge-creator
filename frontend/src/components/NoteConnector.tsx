/**
 * SVG overlay that draws a spline connector between a note anchor in the editor
 * and the corresponding note card in the right panel.
 */
// SYSTEM: note-connector — visual spline between editor anchor and note card

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useEditorView, getRoleView, subscribeRoleView, type EditorRole } from '../editor/active-editor';
import { useNoteStore } from '../store/note-store';
import { useAppStore } from '../store/app-store';
import { useNoteChatStore } from '../store/note-chat-store';
import { useDocState, useUIStore } from '../store/ui-store';
import { readRefOpenMode, showsBothPanes, refIsScope } from '../store/ui-store/documents-slice';

interface Coords { x1: number; y1: number; x2: number; y2: number }


function findAnchorCoords(noteId: string, getView: () => import('@codemirror/view').EditorView | null): { x: number; y: number } | null {
  const view = getView();
  if (!view) return null;
  const docText = view.state.doc.toString();
  const needle = `note:${noteId})`;
  const urlIdx = docText.indexOf(needle);
  if (urlIdx === -1) return null;

  let labelStart = urlIdx - 1;
  while (labelStart > 0 && docText[labelStart - 1] !== '[') labelStart--;
  if (labelStart <= 0) return null;

  const bracketPos = labelStart - 1;
  if (docText[bracketPos] !== '[') return null;

  const startCoords = view.coordsAtPos(labelStart);
  if (!startCoords) return null;

  let rightEdge = startCoords.right;
  let prevTop = startCoords.top;
  for (let pos = labelStart + 1; pos < urlIdx; pos++) {
    const c = view.coordsAtPos(pos);
    if (!c) break;
    if (Math.abs(c.top - prevTop) > 2) break;
    rightEdge = c.right;
    prevTop = c.top;
  }

  return { x: rightEdge, y: (startCoords.top + startCoords.bottom) / 2 };
}

function findPanelCoords(): { x: number; y: number } | null {
  const el = document.querySelector('aside.right-panel');
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  return { x: rect.left, y: (rect.top + rect.bottom) / 2 };
}

function buildPath(c: Coords): string {
  const dx = c.x2 - c.x1;
  const cpOffset = Math.abs(dx) * 0.4;
  return `M${c.x1},${c.y1} C${c.x1 + cpOffset},${c.y1} ${c.x2 - cpOffset},${c.y2} ${c.x2},${c.y2}`;
}

export function NoteConnector() {
  const getEditorView = useEditorView();
  const connectedNoteId = useNoteStore(s => s.connectedNoteId);
  const activeNoteThreadId = useNoteStore(s => s.activeNoteThreadId);
  const currentDocument = useAppStore(s => s.currentDocument);
  const rawReference = useAppStore(s => s.currentReference);
  const sessions = useNoteChatStore(s => s.sessions);
  const refOpenMode = readRefOpenMode(useDocState(currentDocument?.document_id ?? null), useUIStore(s => s.compactLayout));
  // Same projection as useNoteCrud/NotesPanel: a panel-mode previewed reference
  // is not the notes scope, so the connector resolves against the document only.
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  const rightPanelTab = useDocState(currentDocument?.document_id ?? null).rightPanelTab;
  const [coords, setCoords] = useState<Coords | null>(null);
  const rafRef = useRef<number>(0);
  const prevDocIdRef = useRef<string | null>(null);

  const visible = connectedNoteId && rightPanelTab === 'notes';

  // ARCH: resolve the connected note's OWNING
  // column by role so the connector keeps pointing at the owning anchor when the
  // user moves keyboard focus to the OTHER column. Outside split the focused view
  // is used (single visible material). owningRole is null in doc mode, single-ref
  // mode, or when the connected note's session isn't loaded yet.
  const isSplitMode = !!currentReference && !!currentDocument && showsBothPanes(refOpenMode);
  const owningRole: EditorRole | null = (() => {
    if (!isSplitMode || !connectedNoteId || !currentReference) return null;
    const session = sessions.find(s => s.session_id === connectedNoteId);
    if (!session) return null;
    return session.reference_id === currentReference.reference_id ? 'secondary' : 'primary';
  })();
  // Tick that bumps whenever any role view mounts/unmounts, so the connector
  // recomputes promptly once the (possibly lazily-mounted) secondary view is ready.
  const [roleViewTick, setRoleViewTick] = useState(0);

  useEffect(() => {
    if (!visible) {
      setCoords(null);
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
      return;
    }

    const compute = () => {
      // In split resolve the owning column's view by role; otherwise the focused
      // view. findAnchorCoords returns null when the owning view isn't mounted yet
      // → connector hidden until ready (the role-view tick + rAF loop re-resolve it).
      const view = owningRole ? getRoleView(owningRole) : getEditorView();
      const anchor = findAnchorCoords(connectedNoteId!, () => view);
      const panel = findPanelCoords();
      if (anchor && panel) {
        setCoords({ x1: anchor.x, y1: anchor.y, x2: panel.x, y2: panel.y });
      } else {
        setCoords(null);
      }
      rafRef.current = requestAnimationFrame(compute);
    };
    compute();

    return () => {
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
    };
    // owningRole + roleViewTick ensure recompute when the owning column/role view
    // changes; the rAF loop re-queries the live view each frame in between.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectedNoteId, visible, getEditorView, owningRole, roleViewTick]);

  // Subscribe to role-view changes — bumps roleViewTick so the compute effect
  // restarts and resolves the anchor as soon as the secondary column mounts.
  useEffect(() => subscribeRoleView(() => setRoleViewTick(t => t + 1)), []);

  // Merged connector lifecycle: clear on doc switch, auto-connect on tab,
  // clear when thread gone. Dep array is the union of the three original
  // effects — accept a few extra no-op re-runs.
  // INVARIANT: branch order is load-bearing — doc-switch clear must precede
  // auto-connect, otherwise we'd reconnect to a stale doc's thread before
  // clearing it. Final guard runs last so it observes the latest connection.  Why: doc-switch clear must run before auto-connect (else it reconnects to a stale thread); the final guard runs last to observe the latest connection state.
  useEffect(() => {
    const docId = currentDocument?.document_id ?? null;

    if (prevDocIdRef.current !== null && docId !== prevDocIdRef.current && connectedNoteId) {
      useNoteStore.getState().setConnectedNoteId(null);
    }
    prevDocIdRef.current = docId;

    if (activeNoteThreadId && !connectedNoteId && rightPanelTab === 'notes') {
      useNoteStore.getState().setConnectedNoteId(activeNoteThreadId);
    }

    if (!activeNoteThreadId && connectedNoteId) {
      useNoteStore.getState().setConnectedNoteId(null);
    }
  }, [currentDocument?.document_id, connectedNoteId, activeNoteThreadId, rightPanelTab]);

  if (!visible || !coords) return null;

  return createPortal(
    <svg className="fixed inset-0 w-full h-full pointer-events-none z-[12]">
      <path
        d={buildPath(coords)}
        fill="none"
        stroke="rgba(200, 160, 20, 0.85)"
        strokeWidth={2}
        strokeDasharray="6 4"
      />
      <circle cx={coords.x1} cy={coords.y1} r={4} fill="rgba(200, 160, 20, 1)" />
      <circle cx={coords.x2} cy={coords.y2} r={4} fill="rgba(200, 160, 20, 1)" />
    </svg>,
    document.body,
  );
}
