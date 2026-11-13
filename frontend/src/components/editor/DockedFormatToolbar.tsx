/**
 * The formatting toolbar docked above the editor area, shown while the project's
 * `editor_toolbar` setting is on. Acts on whichever editor holds focus (document,
 * reference column, table cell) via editor/active-editor; disabled, not hidden, when
 * there is none. Store slices: currentProject, accessLevel, isPublicShare.
 * Events consumed: editor-selection-change.
 */

import type React from 'react';
import { useState, useEffect, useCallback } from 'react';
import { EditorView } from '@codemirror/view';
import { FormattingButtons, detectActiveStyles, NO_STYLES, type ActiveStyles } from './FormattingButtons';
import { isPanelPreviewView } from '../../editor/editor-plugins';
import { isArtifactSourceView } from './artifact-source-extensions';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { on, off } from '../../events';
import { useEditorView, subscribeRootView } from '../../editor/active-editor';
import { useTranslation } from '../../i18n';

/** True when the docked bar replaces the floating formatting bar for this viewer. */
export function useDockedToolbarOn(): boolean {
  const project = useAppStore(s => s.currentProject);
  const accessLevel = useAppStore(s => s.accessLevel);
  const isPublicShare = useUIStore(s => s.isPublicShare);
  // WHY full only: formatting writes the text; a commentator keeps the floating note bar.
  return !!project && (project.editor_toolbar ?? true) && accessLevel === 'full' && !isPublicShare;
}

/** A view the formatting actions may write through. */
function isFormattable(view: EditorView | null): view is EditorView {
  return !!view
    && view.state.facet(EditorView.editable)
    && !isArtifactSourceView(view)
    && !isPanelPreviewView(view);
}

export function DockedFormatToolbar() {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const [view, setView] = useState<EditorView | null>(null);
  const [styles, setStyles] = useState<ActiveStyles>(NO_STYLES);
  // The selection is read at render (empty → selection-only buttons disabled), so a
  // caret move with unchanged styles must still re-render.
  const [, setTick] = useState(0);

  const refresh = useCallback(() => {
    const v = getEditorView();
    const usable = isFormattable(v) ? v : null;
    setView(usable);
    setStyles(usable ? detectActiveStyles(usable) : NO_STYLES);
    setTick(n => n + 1);
  }, [getEditorView]);

  useEffect(() => {
    refresh();
    on('editor-selection-change', refresh);
    // WHY the root-view feed: a document switch destroys the view with no selection event,
    // and a click before the next focus would act on the dead one.
    const unsubscribe = subscribeRootView(refresh);
    return () => { off('editor-selection-change', refresh); unsubscribe(); };
  }, [refresh]);

  return (
    <div
      role="toolbar"
      aria-label={t('formattingToolbar')}
      // WHY: a press on the bar must not take focus, or the selection collapses first.
      onMouseDown={(e: React.MouseEvent) => e.preventDefault()}
      className="flex flex-wrap items-center justify-center gap-px px-1 py-0.5 shrink-0 bg-surface border-b border-border"
    >
      <FormattingButtons view={view} styles={styles} disabled={!view} onAfterAction={refresh} onSelectionAction={refresh} />
    </div>
  );
}
