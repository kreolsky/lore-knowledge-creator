/**
 * Find & Replace right-panel tab. Drives the live EditorView's CM6 search state
 * (search() extension) via setSearchQuery + the @codemirror/search commands.
 *
 * ARCH: Replaces the stock CM6 bottom search bar. Mounted only while the ephemeral
 * 'find' tab is active (see ProjectPage). Reads the focused EditorView via
 * editor/active-editor, so it targets whichever split-view column has focus.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { EditorView } from '@codemirror/view';
import { setSearchQuery, findNext, findPrevious, selectMatches, replaceNext, replaceAll, getSearchQuery } from '@codemirror/search';
import { Search, Replace } from 'lucide-react';
import { Button, FieldInput, FieldCheckbox, SectionHeader } from '../ui';
import { useEditorView } from '../../editor/active-editor';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { useEvent } from '../../hooks/useEvent';
import { useTranslation } from '../../i18n';
import { buildSearchQuery, type FindState } from './find-query';
import { searchMatchesField, searchPanelListenerCompartment } from './find-matches';

export function FindReplacePanel() {
  const { t } = useTranslation();
  const getView = useEditorView();
  const accessLevel = useAppStore(s => s.accessLevel);
  const canReplace = accessLevel === 'full';

  const [search, setSearch] = useState(() => useUIStore.getState().findSeedText);
  const [replace, setReplace] = useState('');
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [regexp, setRegexp] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);

  // Match-count readout (N/total). Reads the SAME cached field the searchHighlight
  // plugin decorates from — no second full-doc walk. Shallow-compares so unchanged
  // counts don't trigger a re-render (the listener fires on every doc/selection change).
  const [counts, setCounts] = useState({ total: 0, current: 0, truncated: false });
  const recompute = useCallback(() => {
    const view = getView();
    const next = { total: 0, current: 0, truncated: false };
    if (view) {
      const { ranges, truncated } = view.state.field(searchMatchesField, false) ?? { ranges: [], truncated: false };
      const sel = view.state.selection.main;
      next.total = ranges.length;
      // 1-based index of the match under the primary selection; 0 when not on a match.
      next.current = ranges.findIndex(m => m.from === sel.from && m.to === sel.to) + 1;
      next.truncated = truncated;
    }
    setCounts(prev =>
      prev.total === next.total && prev.current === next.current && prev.truncated === next.truncated
        ? prev : next);
  }, [getView]);

  // ARCH: Focus targets for Enter cycling. After Enter/Shift+Enter in the search input the
  // focus moves onto the matching Next/Previous button so subsequent Enter presses keep
  // cycling via the button's native onClick. runCommand deliberately does NOT call
  // view.focus() — that was stealing focus into the editor, making the next Enter insert a
  // newline into the document.
  const prevBtnRef = useRef<HTMLButtonElement>(null);
  const nextBtnRef = useRef<HTMLButtonElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // ARCH: Cmd+F re-seed handling. On fresh open this panel mounts AFTER the open-find
  // emit, so it reads findSeedText once (above) and pushes the seeded query to the view
  // here. On repeat Cmd+F the panel is already mounted and receives the open-find event
  // live below — both paths overwrite the field with the current editor selection.
  useEffect(() => {
    // Mount-only: seed the CM6 search state so matches highlight on open. applyQuery
    // is initialized during render (before effects run), so it is safe to call here.
    applyQuery({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live count: attach an updateListener via the compartment (leak-free: cleared on
  // unmount). Fires recompute on query/doc/selection changes — the selection move from
  // findNext/Prev is the one a React-state-driven recompute would miss.
  useEffect(() => {
    const view = getView();
    if (!view) return;
    recompute();
    const listener = EditorView.updateListener.of((u) => {
      if (u.docChanged || u.selectionSet ||
          !getSearchQuery(u.state).eq(getSearchQuery(u.startState))) {
        recompute();
      }
    });
    view.dispatch({ effects: searchPanelListenerCompartment.reconfigure(listener) });
    return () => {
      const v = getView();
      if (v) v.dispatch({ effects: searchPanelListenerCompartment.reconfigure([]) });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Push the current query into the view's CM6 search state. Accepts overrides so a
  // single input/checkbox change applies before React state has re-rendered.
  const applyQuery = useCallback((overrides: Partial<FindState>) => {
    const view = getView();
    if (!view) return;
    const state: FindState = { search, replace, caseSensitive, regexp, wholeWord, ...overrides };
    view.dispatch({ effects: setSearchQuery.of(buildSearchQuery(state)) });
  }, [getView, search, replace, caseSensitive, regexp, wholeWord]);

  // Repeat Cmd+F (panel already mounted): overwrite the field with the selection and refocus.
  useEvent('open-find', useCallback(({ selection }: { selection: string }) => {
    setSearch(selection);
    applyQuery({ search: selection });
    searchInputRef.current?.focus();
  }, [applyQuery]));

  const runCommand = useCallback((cmd: (view: EditorView) => boolean) => {
    const view = getView();
    if (!view) return;
    cmd(view);
  }, [getView]);

  // INVARIANT: replace operations require 'full' access — guard the handler, never
  // rely on hiding the row alone. Why: backend collab enforces write access on the
  // resulting OT edits, but a no-op here avoids a misleading failed dispatch.
  const runReplace = useCallback((cmd: (view: EditorView) => boolean) => {
    if (!canReplace) return;
    runCommand(cmd);
  }, [canReplace, runCommand]);

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
                runCommand(findPrevious);
                prevBtnRef.current?.focus();
              } else {
                runCommand(findNext);
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
          <Button ref={prevBtnRef} variant="ghost" size="sm" title={t('findPrevious')} onClick={() => runCommand(findPrevious)}>{t('findPrevious')}</Button>
          <Button ref={nextBtnRef} variant="ghost" size="sm" title={t('findNext')} onClick={() => runCommand(findNext)}>{t('findNext')}</Button>
          <Button variant="ghost" size="sm" title={t('findAll')} onClick={() => runCommand(selectMatches)}>{t('findAll')}</Button>
        </div>
        <div className="col-span-2 pl-3.5 mt-2 flex items-center gap-3 text-xs text-text-muted">
          <FieldCheckbox checked={caseSensitive} label={t('optCaseSensitive')} title={t('optCaseSensitiveHint')} onChange={v => { setCaseSensitive(v); applyQuery({ caseSensitive: v }); }} />
          <FieldCheckbox checked={regexp} label={t('optRegexp')} title={t('optRegexpHint')} onChange={v => { setRegexp(v); applyQuery({ regexp: v }); }} />
          <FieldCheckbox checked={wholeWord} label={t('optWholeWord')} title={t('optWholeWordHint')} onChange={v => { setWholeWord(v); applyQuery({ wholeWord: v }); }} />
        </div>

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
              <Button variant="ghost" size="sm" title={t('replaceOne')} onClick={() => runReplace(replaceNext)}>{t('replaceOne')}</Button>
              <Button variant="ghost" size="sm" title={t('replaceAll')} onClick={() => runReplace(replaceAll)}>{t('replaceAll')}</Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
