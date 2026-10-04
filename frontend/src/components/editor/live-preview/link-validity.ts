/** Module-level Sets for link validity — bridge between React stores and CM6 plugins. */
// ARCH: Module-level Sets (not React state) so CM6 plugins can read them without React context.
// Populated by useEditorReferenceSync, consumed by build-structural (broken classes)
// and editor-plugins (click-time validation + toast).

export const validDocIds = new Set<string>();
export const validNoteThreadIds = new Set<string>();
export const validRefIds = new Set<string>();

// INVARIANT: projectRefIds holds ref ids RESOLVED FROM THE OPEN DOCUMENT'S TEXT —
// never a project-wide list. Why: references are project-wide and linkable across
// documents (see LinkSuggestionsPopup), so a ref attached to a sibling document must
// not render/behave as broken — and the project LIST is capped (limit ≤1000), so a
// project-wide fetch would render refs past the cap as broken.
// useEditorReferenceSync collects the ref: ids present in the open doc's text and
// resolves exactly those via POST /references/resolve; storeReferences /
// transcludeMap stay ancestor-scoped. missingRefIds memoizes the server's "absent"
// verdict per id so a missing link is not re-asked on every keystroke;
// ws:reference_created / ws:reference_deleted invalidate both sets
// (useReferenceEvents → 'ref-links-invalidate').
export const projectRefIds = new Set<string>();
export const missingRefIds = new Set<string>();
