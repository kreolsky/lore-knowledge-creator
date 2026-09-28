// GENERATED FILE — DO NOT EDIT. Regenerate with:
//   python3 .claude/scripts/transclusion-grammar-codegen.py --write
// Source of truth: backend/transclusion_grammar.py::SCHEME_TABLE.
// Drift gate: .gitea/workflows/ci.yml → structure-gates → --check.

/** Targets that are NEVER a transclusion (projected from SCHEME_TABLE). */
export const NON_TRANSCLUSION_RE = /^(http:|https:|mailto:|#|note:|data:)/;

/** Transclusion scheme names (projected from SCHEME_TABLE). */
export type TransclusionScheme = 'ref' | 'doc' | 'table' | 'bare-doc';
