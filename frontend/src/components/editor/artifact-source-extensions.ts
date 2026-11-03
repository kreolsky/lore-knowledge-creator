/**
 * CM6 extensions for the Source view of an artifact (an html page) — a plain
 * code editor bound to the page's ytext, deliberately NOT the markdown stack
 * (live preview, smart quotes and auto-fence would rewrite the markup).
 *
 * The two compartments are the document editor's own: the editable effect in
 * Editor.tsx's hook wiring and useEditorCollab's yCollab binding reconfigure
 * whichever view the editor holds, so this view gets the same online-only
 * editing gate and the same CRDT binding.
 *
 * The Source view is also the human writer's half of the artifact size cap
 * (backend/artifacts.py owns the number): a transaction that would grow the
 * source past the cap is refused here and reported through `onOversize`.
 */
import type { Compartment, Extension } from '@codemirror/state';
import { EditorState, Transaction } from '@codemirror/state';
import { EditorView, keymap, highlightSpecialChars, drawSelection } from '@codemirror/view';
import { history, defaultKeymap, historyKeymap, indentWithTab } from '@codemirror/commands';
import { HighlightStyle, syntaxHighlighting, bracketMatching, indentOnInput } from '@codemirror/language';
import { closeBrackets, closeBracketsKeymap, autocompletion, completionKeymap } from '@codemirror/autocomplete';
import { html } from '@codemirror/lang-html';
import { tags } from '@lezer/highlight';
import { ySyncAnnotation } from 'y-codemirror.next';
import { editableCompartment } from '../../editor/editor-plugins';

// WHY colors (not classes, unlike code-highlight.ts): this view holds only code, so
// there is no prose for a tag to leak into; the --code-* variables follow the theme.
const artifactHighlightStyle = HighlightStyle.define([
  { tag: [tags.tagName, tags.angleBracket], color: 'var(--code-keyword)' },
  { tag: tags.attributeName, color: 'var(--code-key)' },
  { tag: [tags.string, tags.attributeValue], color: 'var(--code-string)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--code-number)' },
  { tag: [tags.keyword, tags.operatorKeyword, tags.definitionKeyword], color: 'var(--code-keyword)' },
  { tag: [tags.typeName, tags.className, tags.propertyName], color: 'var(--code-type)' },
  { tag: [tags.comment, tags.meta], color: 'var(--code-comment)' },
]);

/** Marks the Source view's DOM, so selection UI built for prose can stand down. */
export const ARTIFACT_SOURCE_ATTR = 'data-artifact-source';

export function isArtifactSourceView(view: EditorView): boolean {
  return view.dom.hasAttribute(ARTIFACT_SOURCE_ATTR);
}

/**
 * The human writer's size cap: refuse a USER transaction that would GROW the
 * source past `getMaxBytes()` (UTF-8), reporting each refusal via `onOversize`.
 *
 * INVARIANT(data-loss): only user input that grows the doc is filtered —
 * never a remote yCollab transaction, never the collab bind's whole-doc
 * replace (a programmatic dispatch with no userEvent), never a shrink. Why:
 * dropping a remote or bind change desyncs this view from the ydoc and the
 * next edit lands at wrong offsets; refusing a shrink locks an over-cap page
 * (two members growing it concurrently) against the very edit that fixes it.
 * The server-side cap is the authority for every other writer. The cap and
 * the report are read at transaction time (getters, not captured values) so
 * a refreshed /me lands without rebuilding the extension array, which is
 * created once per editor.
 */
function artifactSizeCap(
  getMaxBytes: () => number | null,
  onOversize: (maxBytes: number) => void,
): Extension {
  const encoder = new TextEncoder();
  return EditorState.changeFilter.of(tr => {
    if (tr.annotation(ySyncAnnotation) || !tr.annotation(Transaction.userEvent)) return true;
    const maxBytes = getMaxBytes();
    if (maxBytes == null || !tr.docChanged) return true;
    const doc = tr.newDoc;
    // Cheap bound before the O(n) encode: no UTF-16 unit encodes to more than
    // three UTF-8 bytes (a lone surrogate becomes the 3-byte U+FFFD).
    if (doc.length <= maxBytes / 3) return true;
    const byteLength = (text: typeof doc) => encoder.encode(text.sliceString(0)).length;
    const newBytes = byteLength(doc);
    if (newBytes <= maxBytes) return true;
    // Over the cap: growth is refused, a non-growing edit (in bytes — an
    // equal-length ASCII→Cyrillic swap still grows) passes.
    if (newBytes <= byteLength(tr.startState.doc)) return true;
    onOversize(maxBytes);
    return false;
  });
}

export function buildArtifactSourceExtensions(
  yjsCompartment: Compartment,
  getMaxBytes: () => number | null,
  onOversize: (maxBytes: number) => void,
): Extension[] {
  return [
    highlightSpecialChars(),
    history(),
    drawSelection(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    autocompletion(),
    html(),
    syntaxHighlighting(artifactHighlightStyle),
    EditorView.lineWrapping,
    EditorView.editorAttributes.of({ [ARTIFACT_SOURCE_ATTR]: '' }),
    artifactSizeCap(getMaxBytes, onOversize),
    // INVARIANT(data-loss): seeded non-editable; the editable effect in Editor.tsx opens
    // it once the collab phase is live. Why: same as the document view — keystrokes
    // before the yCollab bind are wiped by the bind's whole-doc replace.
    editableCompartment.of(EditorView.editable.of(false)),
    yjsCompartment.of([]),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...completionKeymap, indentWithTab]),
  ];
}
