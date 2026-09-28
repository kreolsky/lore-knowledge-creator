/** Priority map for the single-active floating-popup system. Higher wins; equal => newest wins. */
// SYSTEM: popups — declarative popup-id → priority for the single-active-popup slot
// ARCH: Only one floating popup is visible at a time. A popup requesting the slot takes over
// any active popup whose priority is <= its own (equal => newest wins) and is suppressed
// against a higher-priority active one. There is NO queue: closing a higher popup does NOT
// restore a previously-suppressed lower one — it reappears only on a fresh user action.
// Gaps of 10 leave room for future popups to slot between existing tiers.
// Mirrors the hotkeys registry shape (see editor/hotkey-config.ts).

export type PopupId =
  | 'hover-preview'        // DocumentTree + ReferencesPanel + EditorLinkPreview (shared lowest tier)
  | 'content-picker'       // chat ContentPickerPopup
  | 'link-suggestions'     // editor LinkSuggestionsPopup
  | 'parent-picker';       // ParentPickerPopup (all call sites)

export type PopupPriorityConfig = Record<PopupId, number>;

export const POPUP_PRIORITY: PopupPriorityConfig = {
  'hover-preview':    10,
  'content-picker':   20,
  'link-suggestions': 20,
  'parent-picker':    30,
};
