/**
 * Split view: document (left) + reference OR table (right) as two editable columns sharing
 * one full-width banner, separated by a draggable ratio divider.
 *
 * Store slices: splitRatio (global), accessLevel, currentReference, documents.
 */
// INVARIANT: in split mode the left column is always the document; the right is the focused
// secondary entity — a reference OR a table (mutually exclusive). The right column appears
// only when one is selected, and a single full-width banner on top owns the exit action.  Why: left=document, right=reference OR table (mutually exclusive); one top banner owns exit so there's one escape handle regardless of right-column type.
// Why: split-view spec (two columns on wide monitors, shared exit banner).

import { useRef, useState, useEffect, useCallback } from 'react';
import { Editor } from '../Editor';
import { ReferenceViewerBanner } from './ReferenceViewerBanner';
import { ReferenceMediaBar } from './ReferenceMediaBar';
import { TableFocusView, TableFocusBanner } from './TableFocusView';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import type { Document, Reference } from '../../types';
import type { CurrentTable } from '../../store/app-store';

interface Props {
  document: Document;
  /** The right-pane reference (mutually exclusive with `table`). */
  reference?: Reference;
  /** The right-pane table (mutually exclusive with `reference`). */
  table?: CurrentTable;
}

export function SplitEditorLayout({ document: doc, reference, table }: Props) {
  const ratio = useUIStore(s => s.splitRatio);
  const setSplitRatio = useUIStore(s => s.setSplitRatio);
  const accessLevel = useAppStore(s => s.accessLevel);
  const setCurrentReference = useAppStore(s => s.setCurrentReference);
  const setCurrentTable = useAppStore(s => s.setCurrentTable);
  const currentTableLabel = useAppStore(s => s.currentTableLabel);
  const documents = useAppStore(s => s.documents);
  const referenceSourceDocId = useAppStore(s => s.referenceSourceDocId);

  const containerRef = useRef<HTMLDivElement>(null);
  const resizerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);

  // WHY: persist the ratio only on pointer-up — setSplitRatio writes global prefs
  // immediately, so persisting on every pointermove would flood the prefs endpoint.
  const [liveRatio, setLiveRatio] = useState(ratio);
  const liveRatioRef = useRef(liveRatio);
  liveRatioRef.current = liveRatio;

  useEffect(() => {
    if (!draggingRef.current) setLiveRatio(ratio);
  }, [ratio]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    draggingRef.current = true;
    resizerRef.current?.classList.add('dragging');
    window.document.body.style.cursor = 'col-resize';
    window.document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!draggingRef.current) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const r = Math.min(0.8, Math.max(0.2, (e.clientX - rect.left) / rect.width));
      setLiveRatio(r);
    };
    const up = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      resizerRef.current?.classList.remove('dragging');
      window.document.body.style.cursor = '';
      window.document.body.style.userSelect = '';
      setSplitRatio(liveRatioRef.current);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, [setSplitRatio]);

  const isReadonly = accessLevel !== 'full';
  // No-empty-flash (item 7a): a content-less ref (body still lazy-fetching) is LOADING,
  // not empty — keep the banner's empty affordance off until the body lands or is
  // confirmed absent. has_content === false (or empty content) is the only true-empty.
  const isRefLoading = !!reference && reference.content === undefined && reference.has_content !== false;
  const isEmpty = !isRefLoading && !reference?.content?.trim();
  const backLabel = referenceSourceDocId
    ? documents.find(d => d.document_id === referenceSourceDocId)?.title
    : undefined;

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-w-0 bg-bg">
      {reference ? (
        <ReferenceViewerBanner
          reference={reference}
          isReadonly={isReadonly}
          isEmpty={isEmpty}
          backLabel={backLabel}
          onBack={() => setCurrentReference(null)}
        />
      ) : (
        <TableFocusBanner label={currentTableLabel ?? ''} onBack={() => setCurrentTable(null)} />
      )}
      <div ref={containerRef} className="flex-1 flex overflow-hidden min-h-0">
        <div className="flex flex-col overflow-hidden min-w-0" style={{ flexGrow: liveRatio, flexBasis: 0 }}>
          <Editor entity={doc} role="primary" hideBanner />
        </div>
        <div ref={resizerRef} className="resizer" onPointerDown={onPointerDown} />
        <div className="flex flex-col overflow-hidden min-w-0" style={{ flexGrow: 1 - liveRatio, flexBasis: 0 }}>
          {/* Audio player bar / archive card live above the reference text only (mirrors
              the centered view); the archive card is the file ref's only download/delete
              surface. Images are excluded: they already preview inside the secondary
              editor's scroll area (Editor's own ReferenceMediaBar), so adding one here
              would double-render the image. ReferenceMediaBar self-nulls when file_path
              is absent, so a file-less ref renders no bar (correct empty state). */}
          {reference && (reference.media_type === 'audio' || reference.media_type === 'file') && (
            <ReferenceMediaBar reference={reference} canEdit={!isReadonly} />
          )}
          {reference ? (
            <Editor entity={reference} role="secondary" hideBanner />
          ) : (
            <TableFocusView table={table!} hideBanner />
          )}
        </div>
      </div>
    </div>
  );
}
