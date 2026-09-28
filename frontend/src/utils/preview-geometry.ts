/** Single source of truth for the LinkPreviewPopup dimensions.
 *
 *  Every preview in the app (sidebar/editor-link/chat-source hover, and the three
 *  picker popups) renders through ONE component — LinkPreviewPopup. Its width and
 *  height caps live here so a "make the preview N% bigger" change touches one line
 *  and cannot desync the viewport-clamp math (popup-position.ts) from the rendered
 *  width. Importers must NEVER redeclare these as locals — that is exactly the
 *  drift bug (clamp used 280 while the popup rendered 364) this module exists to kill. */
// SYSTEM: preview-geometry — single source for LinkPreviewPopup dimensions

/** Rendered popup width. popup-position.ts reads the same symbol for clamp math. */
export const PREVIEW_WIDTH = 380;

/** Gap between the popup and its anchor (hover) or the picker list box (beside-box). */
export const PREVIEW_GAP = 8;

/** Full standalone preview max height — hover popups + the tabbed ContentPicker. */
export const PREVIEW_MAX_HEIGHT = 400;

/** Preview rendered beside a picker list (height matched to the list box, not the
 *  full preview) — LinkSuggestions and ParentPicker. */
export const PREVIEW_PICKER_MAX_HEIGHT = 350;
