/** Route mousedown in .cm-scroller padding into a real CM6 drag-select. */
// SYSTEM: padding-click-focus — fixes selection toolbar when drag starts in scroller padding
// ARCH: CM6 mouse input handlers live on .cm-content. A mousedown landing on
// .cm-scroller (the 2rem padding gap between text column and outer bounds) bypasses
// them entirely: the browser paints a native DOM selection on drag, but CM6's
// SelectionObserver never syncs (contentDOM isn't focused / mousedown wasn't on it),
// so state.selection stays collapsed and downstream consumers (SelectionToolbar via
// editor-observer) never see a non-empty selection. We attach a mousedown listener
// directly on scrollDOM (EditorView.domEventHandlers attaches to contentDOM and
// would not fire here), translate coords via posAtCoords, place the caret, then
// emulate drag-select by tracking mousemove until mouseup.

import { ViewPlugin, EditorView } from '@codemirror/view';
import { EditorSelection } from '@codemirror/state';

export const paddingClickFocus = ViewPlugin.define((view) => {
  const onMouseDown = (event: MouseEvent) => {
    const target = event.target as Node;
    if (view.contentDOM.contains(target)) return;
    if (!view.scrollDOM.contains(target)) return;

    const anchor = view.posAtCoords({ x: event.clientX, y: event.clientY }, false);
    event.preventDefault();
    view.focus();
    view.dispatch({ selection: EditorSelection.cursor(anchor) });

    const onMove = (mv: MouseEvent) => {
      const head = view.posAtCoords({ x: mv.clientX, y: mv.clientY }, false);
      if (head === view.state.selection.main.head) return;
      view.dispatch({ selection: EditorSelection.range(anchor, head) });
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  view.scrollDOM.addEventListener('mousedown', onMouseDown);

  return {
    destroy() {
      view.scrollDOM.removeEventListener('mousedown', onMouseDown);
    },
  };
});
