/**
 * useRightPanelHoverPreview — hover-preview positioning for the right panel.
 *
 * The popup opens to the LEFT of `.right-panel` (so it doesn't overlap the panel
 * content), floored at 4px. Shared by NotesPanel, ReferencesPanel, and the
 * ChatEmptyList parent-label preview so the hover popups cannot drift apart.
 * // SYSTEM: hover-preview — right-panel positioning variant
 * // ARCH: the left formula + vertical shift live in popup-position.ts
 * (getRightPanelPopupLeft + RIGHT_PANEL_PREVIEW_SHIFT) and are ALSO consumed by
 * EditorLinkPreview for chat links inside .right-panel — one formula, two hosts.
 */
import { useHoverPreview } from './useHoverPreview';
import { getRightPanelPopupLeft, RIGHT_PANEL_PREVIEW_SHIFT } from '../utils/popup-position';

export function useRightPanelHoverPreview() {
  return useHoverPreview({
    shiftOffset: RIGHT_PANEL_PREVIEW_SHIFT,
    getPopupLeft: getRightPanelPopupLeft,
  });
}
