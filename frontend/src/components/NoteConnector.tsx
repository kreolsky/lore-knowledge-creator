/**
 * SVG overlay that draws a spline connector between a note anchor in the editor
 * and the corresponding note card in the right panel.
 */
// SYSTEM: note-connector — visual spline between editor anchor and note card

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { EditorView } from '@codemirror/view';
import { findNoteAnchorRange } from '../editor/note-highlight';
import { noteConnectorListenerCompartment } from '../editor/note-connector-refresh';
import { useEditorView, getRoleView, getRootEditorView, subscribeRoleView, type EditorRole } from '../editor/active-editor';
import { useNoteStore } from '../store/note-store';
import { useAppStore } from '../store/app-store';
import { useNoteChatStore } from '../store/note-chat-store';
import { useDocState, useUIStore } from '../store/ui-store';
import { readRefOpenMode, showsBothPanes, refIsScope } from '../store/ui-store/documents-slice';

export interface Coords { x1: number; y1: number; x2: number; y2: number }


/**
 * Where an anchor outside the rendered viewport sits, from the editor's height map.
 *
 * WHY: CM6 renders only the lines near the visible area, and coordsAtPos returns null
 * for every other position — an anchor scrolled far away would drop the connector,
 * which must keep pointing off-screen toward it. The line's vertical middle is exact;
 * the horizontal position is estimated by character width (no glyphs to measure).
 */
function offscreenCoords(view: EditorView, pos: number): { x: number; y: number } {
  const block = view.lineBlockAt(pos);
  const content = view.contentDOM.getBoundingClientRect();
  const x = Math.min(content.left + (pos - block.from) * view.defaultCharacterWidth, content.right);
  return { x, y: view.documentTop + block.top + block.height / 2 };
}

/** Right edge of the first visual line of an anchored range — where the spline starts. */
function rangeEndCoords(view: EditorView, range: { from: number; to: number } | null): { x: number; y: number } | null {
  if (!range) return null;
  const start = view.coordsAtPos(range.from, 1);
  if (!start) return offscreenCoords(view, range.from);
  let rightEdge = start.right;
  for (let pos = range.from + 1; pos <= range.to; pos++) {
    const c = view.coordsAtPos(pos, -1);
    if (!c || Math.abs(c.top - start.top) > 2) break;
    rightEdge = c.right;
  }
  return { x: rightEdge, y: (start.top + start.bottom) / 2 };
}

export function findAnchorCoords(noteId: string, getView: () => EditorView | null): { x: number; y: number } | null {
  const view = getView();
  if (!view) return null;
  return rangeEndCoords(view, findNoteAnchorRange(view.state, noteId));
}

export function findPanelCoords(): { x: number; y: number } | null {
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
      return;
    }

    const compute = () => {
      // In split resolve the owning column's view by role; otherwise the focused
      // view. findAnchorCoords returns null when the owning view isn't mounted yet
      // → connector hidden until ready (the role-view tick restarts this effect
      // and re-resolves as soon as the secondary column mounts).
      const view = owningRole ? getRoleView(owningRole) : getEditorView();
      const anchor = findAnchorCoords(connectedNoteId!, () => view);
      const panel = findPanelCoords();
      if (anchor && panel) {
        setCoords({ x1: anchor.x, y1: anchor.y, x2: panel.x, y2: panel.y });
      } else {
        setCoords(null);
      }
    };

    // Coalescing scheduler: at most one rAF in flight — a burst of scroll
    // events collapses into a single pre-layout compute. Geometry only changes
    // on scroll/resize/editor updates, so the connector idles between them.
    const schedule = () => {
      if (rafRef.current) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = 0;
        compute();
      });
    };

    schedule();

    // The three geometry-changing inputs: any scroll (capture=true — the
    // editor scroller and the right-panel scroller are different elements
    // and scroll does not bubble), window resize, and the owning editor's
    // own updates (doc + viewport changes; the compartment listener fires
    // on the editor's scroll too). All canceled on unmount / effect restart.
    window.addEventListener('scroll', schedule, true);
    window.addEventListener('resize', schedule);
    // WHY the HOST view outside split, not the focused one: the anchor lives in
    // the document, and a focused table cell (claimNestedView) never sees the
    // edits that move it. Captured once — focus changes do not restart this
    // effect, so cleanup must detach the view it attached to, not today's.
    const listenedView = owningRole ? getRoleView(owningRole) : getRootEditorView();
    // Editor updates: attach via the compartment (the leak-free attach/remove
    // seam — see editor/note-connector-refresh.ts), NOT appendConfig: the
    // connector does not own the view and must detach on unmount.
    listenedView?.dispatch({ effects: noteConnectorListenerCompartment.reconfigure(
      EditorView.updateListener.of(() => schedule()),
    ) });

    return () => {
      window.removeEventListener('scroll', schedule, true);
      window.removeEventListener('resize', schedule);
      listenedView?.dispatch({ effects: noteConnectorListenerCompartment.reconfigure([]) });
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
    };
    // owningRole + roleViewTick restart the listeners when the owning
    // column/role view changes; compute re-queries the live view each event.
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
  return <ConnectorSvg coords={coords} />;
}

/** The connector drawing itself — shared by the member and the public notes surfaces.
 *
 * WHY two layers: the panel-side dot sits exactly on the right panel's resizer line
 * (.resizer, z-index 14) and must read above it, while the spline stays under the
 * header and tab bars (z-index 13) it may cross. */
export function ConnectorSvg({ coords }: { coords: Coords }) {
  return createPortal(
    <>
      <svg className="fixed inset-0 w-full h-full pointer-events-none z-[12]">
        <path
          d={buildPath(coords)}
          fill="none"
          stroke="rgba(200, 160, 20, 0.85)"
          strokeWidth={2}
          strokeDasharray="6 4"
        />
        <circle cx={coords.x1} cy={coords.y1} r={4} fill="rgba(200, 160, 20, 1)" />
      </svg>
      <svg className="fixed inset-0 w-full h-full pointer-events-none z-[15]">
        <circle cx={coords.x2} cy={coords.y2} r={4} fill="rgba(200, 160, 20, 1)" />
      </svg>
    </>,
    document.body,
  );
}
