/**
 * Table focus view — renders ONE document table alone in the center editor, the way a
 * reference opens in focus. Plan: tables-as-references-badges.
 *
 * see SYSTEM: table-block — the single-table center/split editor. A table is a CRDT subtree of
 * the document, NOT a separate entity, so this view does NOT own a collab connection:
 *
 * ARCH (fallback path, resolved by the Task-0 spike): the document `<Editor>` stays mounted
 * (it owns the collab handle + publishes it via useEditorCollab). This view is a SEPARATE
 * local CM6 view whose doc text is exactly `tableAnchor(label, id)`. The live-preview
 * `tableBlockField` (in `renderExtensions()`) turns that single anchor into one
 * `TableBlockWidget`, which binds to the live ydoc by the view's ENTITY
 * (`tableHostEntity` facet → `subscribeEntityHandle`) — so cell
 * edits flow widget → ydoc.tables → collab → peers + the (hidden) inline editor. Why the
 * fallback over owning a second connection: it avoids a collab leave+rejoin (overlay flash,
 * presence reset, access race) on every table open/back, and reuses the exact binding the
 * inline widget already uses.
 *
 * INVARIANT: NO `createYjsExtension` for `content` here — binding the focus view's local
 * single-anchor text to the document's `content` Y.Text would overwrite the real document.  Why: binding the focus view's local single-anchor text to the doc's content Y.Text would overwrite the real document, so no content binding here; the host view's text is local and covered by the block widget.
 * The host view's text is local and never directly edited (the block widget covers it);
 * the only edits are cell edits, which go through the widget's nested editors.
 *
 * ARCH (single-active-editor): while a table is open this view is the active editor. It
 * claims the focus-sensitive slots (focused/root view, role, handle) via `claimFocus` on
 * view creation, and on unmount restores the document editor's view (still mounted under
 * `role='primary'`) so the toolbar/hotkeys/content-capture target the document again.
 */

import { useEffect, useMemo, useRef } from 'react';
import { EditorState, Prec, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { Table, ArrowLeft } from 'lucide-react';
import CodeMirrorEditor from './CodeMirrorEditor';
import { editorExperience, renderExtensions } from './render-bundle';
import { tableAnchor, getTablesMap } from './live-preview/table-block-model';
import { tableHostEntity } from './live-preview/table-doc-source';
import { claimFocus, getRoleView, setViewEntity } from '../../editor/active-editor';
import { editableCompartment } from '../../editor/editor-plugins';
import { getEntityHandle } from '../../collab/active-handle-registry';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { Button } from '../ui';
import type { CurrentTable } from '../../store/app-store';

/** Banner for the table focus view — table icon + label + exit (back to document). */
export function TableFocusBanner({ label, onBack }: { label: string; onBack: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="snapshot-preview-banner table-focus-banner">
      <div className="flex items-center gap-1.5 min-w-0">
        <Table size={14} className="shrink-0 text-text-dim" />
        <span className="text-ui-base text-text-muted truncate" title={label}>{label}</span>
      </div>
      <div className="ml-auto flex items-center gap-1.5">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft size={13} /> {t('tableFocusBack')}
        </Button>
      </div>
    </div>
  );
}

interface TableFocusViewProps {
  table: CurrentTable;
  /** Split secondary-pane mode: suppresses the banner (hoisted to the shared split banner). */
  hideBanner?: boolean;
}

export function TableFocusView({ table, hideBanner = false }: TableFocusViewProps) {
  const { t } = useTranslation();
  const setCurrentTable = useAppStore(s => s.setCurrentTable);
  const currentTableLabel = useAppStore(s => s.currentTableLabel);
  const accessLevel = useAppStore(s => s.accessLevel);
  const previewDocument = useAppStore(s => s.previewDocument);
  const isReadonly = accessLevel !== 'full' || !!previewDocument;
  const viewRef = useRef<EditorView | null>(null);

  // Local doc text = the single anchor. The label is resolved LIVE from the store's
  // `currentTableLabel` (set synchronously on open, kept reactive by the ReferencesPanel).
  // CodeMirrorEditor seeds `doc` only at mount (empty-deps effect) and the block widget
  // covers the raw anchor, so a later rename not re-seeding here is invisible (documented
  // limitation); the visible banner label below DOES update.
  const docText = useMemo(() => tableAnchor(currentTableLabel ?? '', table.table_id), [currentTableLabel, table.table_id]);

  const extensions = useMemo<Extension[]>(() => [
    editorExperience({ mode: 'document' }),
    // Host identity for the table widget's entity late-bind: a FACET (not the
    // view→entity registry) because this view's doc text IS the anchor — the widget's
    // toDOM runs during view construction, before handleCreateView could register.
    tableHostEntity.of(table.document_id),
    renderExtensions(),
    // Shared compartment (same instance the document Editor uses); each EditorView resolves
    // it independently, so the focus view's editable never leaks into the document editor.
    editableCompartment.of(EditorView.editable.of(true)),
    // Re-claim the focus slots whenever this view gains focus (e.g. user clicks back in
    // after interacting with the panel) so the toolbar/hotkeys target the table.
    EditorView.domEventHandlers({
      focus: (_e, view) => {
        claimFocus({
          role: 'primary',
          isReference: false,
          view,
          handle: docHandle(),
        });
        return false;
      },
    }),
  ], []);

  // Gate editability on access level. Cell edits write the live ydoc; Yjs re-syncs the
  // whole doc on reconnect, so a brief disconnect does not lose table edits (unlike plain
  // text input the offline overlay guards) — gating on access level alone is correct here.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: editableCompartment.reconfigure(
        isReadonly
          ? [EditorView.editable.of(false), Prec.highest(EditorState.readOnly.of(true))]
          : [EditorView.editable.of(true)],
      ),
    });
  }, [isReadonly]);

  // Restore the document editor's view as the focused/root source when the table closes.
  // The document Editor stays mounted (under role='primary') while this view is open, so
  // its view is still registered and can be re-claimed.
  useEffect(() => () => {
    const primary = getRoleView('primary');
    if (primary) {
      claimFocus({
        role: 'primary',
        isReference: false,
        view: primary,
        handle: docHandle(),
      });
    }
  }, []);

  // The DOCUMENT's entity handle — the table model lives in the document's ydoc.
  // INVARIANT: no slot fallback. Why: claimFocus WRITES the slot with this value —
  // passing getActiveHandle() through would re-stamp a possibly-FOREIGN handle (the
  // open reference's) as this view's, mis-parenting notes/markdown actions. Null is
  // the honest claim when the entity entry is missing (conn race); readers tolerate it.
  const docHandle = () => getEntityHandle(table.document_id);

  // No-silent-degradation guard: if the table MODEL is removed (local delete via the panel
  // badge, or a collaborative peer delete) the focus view has nothing left to render — close
  // it instead of leaving the user on an orphan-anchor error state. Observes the live ydoc.
  useEffect(() => {
    const handle = getEntityHandle(table.document_id);
    if (!handle?.ydoc) return;
    const tables = getTablesMap(handle.ydoc);
    const cb = (): void => { if (!tables.has(table.table_id)) setCurrentTable(null); };
    cb();
    tables.observe(cb);
    return () => tables.unobserve(cb);
  }, [table.document_id, table.table_id, setCurrentTable]);

  const handleCreateView = (view: EditorView) => {
    viewRef.current = view;
    // Register the view's entity so the block widget late-binds the DOCUMENT's ydoc
    // (getViewEntity → subscribeEntityHandle), not the focused slot.
    setViewEntity(view, table.document_id);
    claimFocus({
      role: 'primary',
      isReference: false,
      view,
      handle: docHandle(),
    });
    if (!isReadonly) view.focus();
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-bg">
      {!hideBanner && (
        <TableFocusBanner label={currentTableLabel ?? t('tableBadgeUntitled')} onBack={() => setCurrentTable(null)} />
      )}
      <div className="editor-scroll" style={{ overflow: 'hidden' }}>
        <div className="editor-content" style={{ height: '100%', minHeight: 0, width: '100%', padding: 0 }}>
          <CodeMirrorEditor
            doc={docText}
            extensions={extensions}
            onCreateView={handleCreateView}
            className="editor-cm table-focus-cm"
          />
        </div>
      </div>
    </div>
  );
}
