/** Module-level Sets for link validity — bridge between React stores and CM6 plugins. */
// ARCH: Module-level Sets (not React state) so CM6 plugins can read them without React context.
// Populated by useEditorReferenceSync, consumed by build-structural (broken classes)
// and editor-plugins (click-time validation + toast).

export const validDocIds = new Set<string>();
export const validNoteThreadIds = new Set<string>();
export const validRefIds = new Set<string>();

// INVARIANT: project-wide reference IDs — a ref: link is valid if its target lives anywhere
// in the project, not just the current doc's ancestor-scoped set (validRefIds).
// Why: references attached to sibling documents are linkable (see LinkSuggestionsPopup), so
// their links must not render/behave as broken. Holds IDs only (cheap), fetched once per
// project by useEditorReferenceSync — keeps storeReferences / transcludeMap ancestor-scoped.
export const projectRefIds = new Set<string>();
