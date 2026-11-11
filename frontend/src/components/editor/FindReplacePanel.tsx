/**
 * Find & Replace right-panel tab. Drives the HOST EditorView's CM6 search state
 * (search() extension) via setSearchQuery + the @codemirror/search commands.
 *
 * ARCH: Replaces the stock CM6 bottom search bar. Mounted only while the ephemeral
 * 'find' tab is active (see ProjectPage). Targets the ROOT (host) view via
 * editor/active-editor — never a focused table cell's nested view — and re-binds
 * when the root slot's view changes (document switch, snapshot-preview enter/exit).
 *
 * ARCH: the match count is the COMBINED list over host text + every anchored
 * table's cells (attachFindIndex in find-matches.ts) — a word inside a table is
 * counted. Next/Prev walk that combined list (own navigation — the stock
 * commands see the host buffer only, so a cell match is unreachable for them);
 * Find-all selects the host matches as one multi-selection (a multi-selection
 * cannot reach nested cell views). Replace acts on the match the user is ON (a
 * cell match through that cell's mounted editor — Ctrl+Z-undoable in place);
 * Replace-all = checkpoint → one transaction over host + cells → a one-shot Undo
 * line (find-replace-undo.ts).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { EditorView } from '@codemirror/view';
import { EditorSelection } from '@codemirror/state';
import { setSearchQuery, getSearchQuery } from '@codemirror/search';
import { Search, Replace } from 'lucide-react';
import { Button, FieldInput, FieldCheckbox, SectionHeader } from '../ui';
import { useRootEditorView, getRootEditorView, subscribeRootView, entityYdoc, getViewEntity } from '../../editor/active-editor';
import { apiClient } from '../../api/client';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { useEvent } from '../../hooks/useEvent';
import { useTranslation } from '../../i18n';
import { buildSearchQuery, type FindState } from './find-query';
import {
  attachFindIndex, collectAnchorSpans, currentCellMatchField, sameSearch, setCurrentCellMatch,
  type FindIndexHandle, type FindMatch,
} from './find-matches';
import { revealCellMatchInView } from './live-preview/table-relays';
import { findTableWidget } from './live-preview/table-widget-registry';
import {
  applyReplaceAll, planReplaceAll, replaceEdits, type ReplaceAllUndo,
} from './find-replace-undo';

/** Index of the match the user is ON: the cell match Next/Prev last landed on
 *  (the host selection cannot represent it), else a host match equal to the host's
 *  primary selection (a click, host navigation, Find-all). -1 = none. */
function currentMatchIndex(view: EditorView, matches: readonly FindMatch[]): number {
  const cur = view.state.field(currentCellMatchField, false);
  if (cur) {
    const i = matches.findIndex(m => m.kind === 'cell' && m.tableId === cur.tableId
      && m.rowId === cur.rowId && m.colId === cur.colId && m.from === cur.from && m.to === cur.to);
    if (i >= 0) return i;
  }
  const sel = view.state.selection.main;
  return matches.findIndex(m => m.kind === 'host' && m.from === sel.from && m.to === sel.to);
}

/**
 * Where a Next/Prev walk starts: the index the first step (`idx + dir`) moves
 * FROM. On a match → that match. Otherwise from the host caret, the way the stock
 * findNext/findPrevious do — a cell match sorts at its table anchor's host position.
 * Nothing past the caret → wraps (the walk's modulo turns -1 / length into the ends).
 */
function walkStart(view: EditorView, matches: readonly FindMatch[], dir: 1 | -1): number {
  const on = currentMatchIndex(view, matches);
  if (on >= 0) return on;
  const sel = view.state.selection.main;
  const anchorAt = new Map(collectAnchorSpans(view.state.doc.toString()).map(a => [a.id, a.from]));
  const at = (m: FindMatch): number => (m.kind === 'host' ? m.from : anchorAt.get(m.tableId) ?? 0);
  if (dir === 1) return startBefore(view, matches, [sel.to, 0, 0]);
  for (let i = matches.length - 1; i >= 0; i--) {
    const m = matches[i];
    if (m.kind === 'host' ? m.to <= sel.from : at(m) < sel.from) return i + 1;
  }
  return 0;
}

/** Document-order key of a match: host [from,0,0]; cell [anchorFrom, 1+ord, from], `ord` =
 *  the cell's row-major index in the table's order (a host match never starts at an
 *  anchor — anchor-crossing ones are dropped). */
type OrderKey = [number, number, number];
function orderKey(m: FindMatch, anchorAt: ReadonlyMap<string, number>): OrderKey {
  return m.kind === 'host' ? [m.from, 0, 0] : [anchorAt.get(m.tableId) ?? 0, 1 + m.ord, m.from];
}
function keyLess(a: OrderKey, b: OrderKey): boolean {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}

/** Walk start (for dir 1) so the first step lands on the first match at/after `key`;
 *  nothing at/after → wraps to the first match. */
function startBefore(view: EditorView, matches: readonly FindMatch[], key: OrderKey): number {
  const anchorAt = new Map(collectAnchorSpans(view.state.doc.toString()).map(a => [a.id, a.from]));
  const i = matches.findIndex(m => !keyLess(orderKey(m, anchorAt), key));
  return i >= 0 ? i - 1 : matches.length - 1;
}

export function FindReplacePanel() {
  const { t } = useTranslation();
  const getView = useRootEditorView();
  const accessLevel = useAppStore(s => s.accessLevel);

  const [search, setSearch] = useState(() => useUIStore.getState().findSeedText);
  const [replace, setReplace] = useState('');
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [regexp, setRegexp] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);

  // Root-view slot: Find targets the host editor. Tracked as state so the panel
  // re-binds (listener + combined index + query push) when the slot's view is
  // replaced — a stale binding holds a listener on a destroyed view and freezes
  // the count. The sync re-read catches a mount that raced the initial state.
  const [rootView, setRootView] = useState(getRootEditorView);
  useEffect(() => {
    setRootView(getRootEditorView());
    return subscribeRootView(setRootView);
  }, []);
  // Replace is hidden for anyone below 'full' AND over a read-only host (the
  // snapshot preview) — the handlers re-check both.
  const canReplace = accessLevel === 'full' && !!rootView && !rootView.state.readOnly;

  // Replace-all's one-shot Undo line. `undoRef` holds the Replace-all's step on the
  // document-wide undo stack; the line state only renders. dropUndo tears both down.
  const [undoLine, setUndoLine] = useState<{ total: number; cells: number } | null>(null);
  const undoRef = useRef<ReplaceAllUndo | null>(null);
  const dropUndo = useCallback(() => {
    undoRef.current?.destroy();
    undoRef.current = null;
    setUndoLine(null);
  }, []);
  // Root slot changed (document switch, preview) or the panel unmounts → the line goes.
  useEffect(() => dropUndo, [rootView, dropUndo]);

  // Match-count readout (N/total) over the COMBINED host+cells list. Shallow-compares
  // so unchanged counts don't trigger a re-render. `cells` drives the Find-all note.
  const [counts, setCounts] = useState({ total: 0, current: 0, truncated: false, cells: 0 });
  const indexRef = useRef<FindIndexHandle | null>(null);
  const recompute = useCallback(() => {
    const view = getRootEditorView();
    const handle = indexRef.current;
    const next = { total: 0, current: 0, truncated: false, cells: 0 };
    if (view && handle) {
      const { matches, truncated } = handle.matches();
      next.total = matches.length;
      next.cells = matches.reduce((n, m) => n + (m.kind === 'cell' ? 1 : 0), 0);
      next.truncated = truncated;
      next.current = currentMatchIndex(view, matches) + 1;
    }
    setCounts(prev =>
      prev.total === next.total && prev.current === next.current
      && prev.truncated === next.truncated && prev.cells === next.cells
        ? prev : next);
  }, []);

  // ARCH: Focus targets for Enter cycling. After Enter/Shift+Enter in the search input the
  // focus moves onto the matching Next/Previous button so subsequent Enter presses keep
  // cycling via the button's native onClick. runCommand deliberately does NOT call
  // view.focus() — that was stealing focus into the editor, making the next Enter insert a
  // newline into the document.
  const prevBtnRef = useRef<HTMLButtonElement>(null);
  const nextBtnRef = useRef<HTMLButtonElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Push the current query into the view's CM6 search state. Accepts overrides so a
  // single input/checkbox change applies before React state has re-rendered.
  const applyQuery = useCallback((overrides: Partial<FindState>) => {
    const view = getView();
    if (!view) return;
    const state: FindState = { search, replace, caseSensitive, regexp, wholeWord, ...overrides };
    const query = buildSearchQuery(state);
    // A query or replacement edit ends the Replace-all's Undo window.
    dropUndo();
    // A new SEARCH drops the cell current-match (its offsets belong to the old match
    // list); a Replace-field edit keeps it — the match set did not change.
    const stale = !sameSearch(getSearchQuery(view.state), query)
      && view.state.field(currentCellMatchField, false);
    view.dispatch({
      effects: [setSearchQuery.of(query), ...(stale ? [setCurrentCellMatch.of(null)] : [])],
    });
  }, [getView, search, replace, caseSensitive, regexp, wholeWord, dropUndo]);
  // Live ref so the [rootView] effect re-pushes the query into a newly bound view
  // without re-attaching on every keystroke (applyQuery's identity churns per input).
  const applyQueryRef = useRef(applyQuery);
  applyQueryRef.current = applyQuery;

  // Live count: attach the view-bound combined index (host transactions via the
  // listener compartment; peer cell edits via the tables-map deep observer — those
  // produce NO host transaction) and seed the view with the current query. Fires
  // recompute through the index's debounced notify, so the count tracks edits made
  // anywhere in the document, cells included.
  useEffect(() => {
    if (!rootView) return;
    const handle = attachFindIndex(rootView, recompute);
    indexRef.current = handle;
    applyQueryRef.current({});
    recompute();
    return () => {
      indexRef.current = null;
      handle.destroy();
    };
  }, [rootView, recompute]);

  // Repeat Cmd+F (panel already mounted): overwrite the field with the selection and refocus.
  useEvent('open-find', useCallback(({ selection }: { selection: string }) => {
    setSearch(selection);
    applyQuery({ search: selection });
    searchInputRef.current?.focus();
  }, [applyQuery]));

  // Own Next/Prev over the COMBINED list (host + cells in document order). The
  // stock commands walk the host buffer only — a cell match is unreachable for
  // them. A cell landing goes through the table widget (mount + select after
  // measure, or scroll + static current-mark where mounting is refused).
  const gotoMatch = useCallback((dir: 1 | -1, startKey?: OrderKey) => {
    const view = getRootEditorView();
    const handle = indexRef.current;
    if (!view || !handle) return;
    let matches = handle.matches().matches;
    if (matches.length === 0) return;
    let idx = startKey ? startBefore(view, matches, startKey) : walkStart(view, matches, dir);
    const walk = async (): Promise<void> => {
      // One attempt per match of the ORIGINAL list: enough to reach every match
      // once, even when dead cells force re-reads of a shrinking list along the way.
      for (let attempts = matches.length; attempts > 0; attempts--) {
        idx = (idx + dir + matches.length) % matches.length;
        const m = matches[idx];
        if (m.kind === 'host') {
          view.dispatch({
            selection: { anchor: m.from, head: m.to },
            effects: setCurrentCellMatch.of(null),
            scrollIntoView: true,
          });
          recompute();
          return;
        }
        // Publish the cell current-match BEFORE reaching: the relay paints the
        // static class / mounted-cell mark while (or before) the cell appears.
        view.dispatch({
          effects: setCurrentCellMatch.of(
            { tableId: m.tableId, rowId: m.rowId, colId: m.colId, from: m.from, to: m.to }),
        });
        if (await revealCellMatchInView(view, m)) {
          recompute();
          return;
        }
        // The root view died mid-walk (document switch re-bound the slot) — stop
        // quietly instead of dispatching into a destroyed view.
        if (getRootEditorView() !== view) return;
        // The cell died under a structural rebuild between listing and reaching
        // it (a peer dropped the row) — re-read the live list and skip to the
        // next live match instead of throwing.
        matches = handle.matches().matches;
        if (matches.length === 0) return;
      }
      view.dispatch({ effects: setCurrentCellMatch.of(null) });
      recompute();
    };
    void walk();
  }, [recompute]);

  // INVARIANT: replace operations require 'full' access on an editable host — the
  // handlers re-check, never rely on hiding the row alone. Why: backend collab
  // enforces write access on the resulting edits; a no-op here avoids a misleading
  // failed write.
  const replaceOne = useCallback(() => {
    const view = getRootEditorView();
    const handle = indexRef.current;
    if (!view || !handle || useAppStore.getState().accessLevel !== 'full' || view.state.readOnly) return;
    const query = getSearchQuery(view.state);
    const { matches } = handle.matches();
    const idx = currentMatchIndex(view, matches);
    // Not on a match yet → the first press moves onto one (the stock replaceNext shape).
    if (idx < 0) { gotoMatch(1); return; }
    const m = matches[idx];
    if (m.kind === 'host') {
      const edit = replaceEdits(query, view.state.doc.toString()).find(e => e.from === m.from && e.to === m.to);
      if (!edit) return;
      const end = m.from + edit.insert.length;
      view.dispatch({
        changes: { from: m.from, to: m.to, insert: edit.insert },
        selection: { anchor: end },
        userEvent: 'input.replace',
      });
      gotoMatch(1, [end, 0, 0]);
      return;
    }
    // A cell match: through the cell's MOUNTED editor (Next mounted it) — an ordinary
    // CM6 dispatch, undoable with Ctrl+Z inside the cell, like typing.
    // Ruling: an unmounted cell (viewer-era static, or a rebuild dropped it) is
    // reached first; the next press replaces.
    const cellView = findTableWidget(view, m.tableId)?.mountedCellView(m.rowId, m.colId) ?? null;
    if (!cellView) { void revealCellMatchInView(view, m); return; }
    const edit = replaceEdits(query, cellView.state.doc.toString()).find(e => e.from === m.from && e.to === m.to);
    if (!edit) return;
    const end = m.from + edit.insert.length;
    cellView.dispatch({
      changes: { from: m.from, to: m.to, insert: edit.insert },
      selection: { anchor: end },
      userEvent: 'input.replace',
    });
    const anchorAt = new Map(collectAnchorSpans(view.state.doc.toString()).map(a => [a.id, a.from]));
    view.dispatch({ effects: setCurrentCellMatch.of(null) });
    gotoMatch(1, [anchorAt.get(m.tableId) ?? 0, 1 + m.ord, end]);
  }, [gotoMatch]);

  // Replace-all: (1) checkpoint of the LIVE doc (content: null → the server captures
  // the Y.Doc incl. tables) — on failure nothing is written; (2) re-derive the edit
  // set synchronously after the await; (3) one REPLACE_ALL_ORIGIN transaction over
  // host + cells = one step on the document-wide undo stack.
  const replaceAllMatches = useCallback(async () => {
    const view = getRootEditorView();
    if (!view || useAppStore.getState().accessLevel !== 'full' || view.state.readOnly) return;
    const ydoc = entityYdoc(view);
    const documentId = useAppStore.getState().currentDocument?.document_id;
    // Ruling: the checkpoint route is per DOCUMENT — a root view on another entity
    // (or no live doc) fails loudly instead of replacing without a backup.
    if (!ydoc || !documentId || getViewEntity(view) !== documentId) {
      useAppStore.getState().showToast(t('replaceAllFailed'), 'error');
      return;
    }
    if (!getSearchQuery(view.state).valid) return;
    dropUndo();
    try {
      await apiClient.post('/checkpoints', { document_id: documentId, content: null, label: t('replaceAllBackupLabel') });
    } catch (err) {
      console.error('Failed to back up before replace all', err);
      useAppStore.getState().showToast(t('replaceAllFailed'), 'error');
      return;
    }
    // The await yielded: the slot may hold another view now — never write into it.
    if (getRootEditorView() !== view || entityYdoc(view) !== ydoc) {
      useAppStore.getState().showToast(t('replaceAllFailed'), 'error');
      return;
    }
    const plan = planReplaceAll(getSearchQuery(view.state), ydoc.getText('content').toString(), ydoc);
    const total = plan.host.length + plan.cellCount;
    if (total === 0) return;
    view.dispatch({ effects: setCurrentCellMatch.of(null) });
    // The line lives while the Replace-all is the top step (see INVARIANT at applyReplaceAll).
    undoRef.current = applyReplaceAll(ydoc, ydoc.getText('content'), plan, dropUndo);
    if (undoRef.current) setUndoLine({ total, cells: plan.cellCount });
    recompute();
  }, [t, dropUndo, recompute]);

  const undoReplaceAll = useCallback(() => {
    undoRef.current?.undo();
    dropUndo();
    recompute();
  }, [dropUndo, recompute]);

  // Find-all: the host matches (already anchor-crossing-free in the combined
  // list) as ONE multi-selection. A multi-selection cannot reach nested cell
  // views — the note under the options row says cells are not selected.
  const findAll = useCallback(() => {
    const view = getRootEditorView();
    const handle = indexRef.current;
    if (!view || !handle) return;
    const { matches } = handle.matches();
    const ranges = matches
      .filter((m): m is Extract<FindMatch, { kind: 'host' }> => m.kind === 'host')
      .map(m => EditorSelection.range(m.from, m.to));
    if (ranges.length === 0) return;
    view.dispatch({
      selection: EditorSelection.create(ranges, 0),
      effects: [
        setCurrentCellMatch.of(null),
        EditorView.scrollIntoView(ranges[0].from, { y: 'center' }),
      ],
    });
    recompute();
  }, [recompute]);

  return (
    // ARCH: A single grid [1fr auto] spans BOTH the find and replace rows so the action
    // column auto-sizes to the WIDEST button group across the two rows. Result: buttons
    // always fit (auto), both inputs share one identical width (1fr), and the layout adapts
    // to per-language button-label lengths without any fixed widths. Section headers are
    // full-bleed col-span-2 rows; col-1 cells carry pl-3.5 so input text lines up with the
    // header text (panel px-1.5 + pl-3.5 = 1.25rem, the SectionHeader's effective indent).
    <div className="h-full overflow-auto py-1 px-1.5">
      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 items-center">
        <div className="col-span-2"><SectionHeader first icon={<Search size={11} />} title={t('sectionFind')} /></div>
        <div className="pl-3.5 min-w-0">
          <FieldInput
            className="w-full"
            ref={searchInputRef}
            value={search}
            autoFocus
            placeholder={t('findPlaceholder')}
            onChange={e => { setSearch(e.target.value); applyQuery({ search: e.target.value }); }}
            onKeyDown={e => {
              if (e.key !== 'Enter') return;
              if (e.shiftKey) {
                gotoMatch(-1);
                prevBtnRef.current?.focus();
              } else {
                gotoMatch(1);
                nextBtnRef.current?.focus();
              }
            }}
          />
        </div>
        <div className="flex items-center gap-1 pr-3.5">
          {counts.total > 0 && (
            <span
              className="text-ui-xs text-text-dim tabular-nums mr-1 select-none"
              title={t('findCountHint')}
            >
              {counts.current} / {counts.truncated ? '1000+' : counts.total}
            </span>
          )}
          <Button ref={prevBtnRef} variant="ghost" title={t('findPrevious')} onClick={() => gotoMatch(-1)}>{t('findPrevious')}</Button>
          <Button ref={nextBtnRef} variant="ghost" title={t('findNext')} onClick={() => gotoMatch(1)}>{t('findNext')}</Button>
          <Button variant="ghost" title={t('findAll')} onClick={findAll}>{t('findAll')}</Button>
        </div>
        <div className="col-span-2 pl-3.5 mt-2 flex items-center gap-3 text-xs text-text-muted">
          <FieldCheckbox checked={caseSensitive} label={t('optCaseSensitive')} title={t('optCaseSensitiveHint')} onChange={v => { setCaseSensitive(v); applyQuery({ caseSensitive: v }); }} />
          <FieldCheckbox checked={regexp} label={t('optRegexp')} title={t('optRegexpHint')} onChange={v => { setRegexp(v); applyQuery({ regexp: v }); }} />
          <FieldCheckbox checked={wholeWord} label={t('optWholeWord')} title={t('optWholeWordHint')} onChange={v => { setWholeWord(v); applyQuery({ wholeWord: v }); }} />
        </div>
        {counts.cells > 0 && (
          <div className="col-span-2 pl-3.5 mt-2 text-ui-xs text-text-muted">
            {t('findAllCellsNotSelected')}
          </div>
        )}

        {canReplace && (
          <>
            <div className="col-span-2"><SectionHeader icon={<Replace size={11} />} title={t('sectionReplace')} /></div>
            <div className="pl-3.5 min-w-0">
              <FieldInput
                className="w-full"
                value={replace}
                placeholder={t('replacePlaceholder')}
                onChange={e => { setReplace(e.target.value); applyQuery({ replace: e.target.value }); }}
              />
            </div>
            <div className="flex items-center gap-1 pr-3.5">
              <Button variant="ghost" title={t('replaceOne')} onClick={replaceOne}>{t('replaceOne')}</Button>
              <Button variant="ghost" title={t('replaceAll')} onClick={() => { void replaceAllMatches(); }}>{t('replaceAll')}</Button>
            </div>
            {undoLine && (
              <div className="col-span-2 pl-3.5 mt-2 flex items-center gap-2 text-ui-xs text-text-muted">
                <span>{t('replaceAllDone', { n: undoLine.total, m: undoLine.cells })}</span>
                <span>·</span>
                <Button variant="ghost" onClick={undoReplaceAll}>{t('replaceAllUndo')}</Button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
