/**
 * CM6 extensions for the Source view of an html page — a plain code editor bound
 * to the page's ytext, deliberately NOT the markdown stack (live preview, smart
 * quotes and auto-fence would rewrite the markup).
 *
 * The two compartments are the document editor's own: Editor.tsx's editable effect
 * and useEditorCollab's yCollab binding reconfigure whichever view the editor holds,
 * so this view gets the same online-only editing gate and the same CRDT binding.
 */
import type { Compartment, Extension } from '@codemirror/state';
import { EditorView, keymap, highlightSpecialChars, drawSelection } from '@codemirror/view';
import { history, defaultKeymap, historyKeymap, indentWithTab } from '@codemirror/commands';
import { HighlightStyle, syntaxHighlighting, bracketMatching, indentOnInput } from '@codemirror/language';
import { closeBrackets, closeBracketsKeymap, autocompletion, completionKeymap } from '@codemirror/autocomplete';
import { html } from '@codemirror/lang-html';
import { tags } from '@lezer/highlight';
import { editableCompartment } from '../../editor/editor-plugins';

// WHY colors (not classes, unlike code-highlight.ts): this view holds only code, so
// there is no prose for a tag to leak into; the --code-* variables follow the theme.
const htmlHighlightStyle = HighlightStyle.define([
  { tag: [tags.tagName, tags.angleBracket], color: 'var(--code-keyword)' },
  { tag: tags.attributeName, color: 'var(--code-key)' },
  { tag: [tags.string, tags.attributeValue], color: 'var(--code-string)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--code-number)' },
  { tag: [tags.keyword, tags.operatorKeyword, tags.definitionKeyword], color: 'var(--code-keyword)' },
  { tag: [tags.typeName, tags.className, tags.propertyName], color: 'var(--code-type)' },
  { tag: [tags.comment, tags.meta], color: 'var(--code-comment)' },
]);

/** Marks the Source view's DOM, so selection UI built for prose can stand down. */
export const HTML_SOURCE_ATTR = 'data-html-source';

export function isHtmlSourceView(view: EditorView): boolean {
  return view.dom.hasAttribute(HTML_SOURCE_ATTR);
}

export function buildHtmlSourceExtensions(yjsCompartment: Compartment): Extension[] {
  return [
    highlightSpecialChars(),
    history(),
    drawSelection(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    autocompletion(),
    html(),
    syntaxHighlighting(htmlHighlightStyle),
    EditorView.lineWrapping,
    EditorView.editorAttributes.of({ [HTML_SOURCE_ATTR]: '' }),
    // INVARIANT(data-loss): seeded non-editable; the editable effect in Editor.tsx opens
    // it once the collab phase is live. Why: same as the document view — keystrokes
    // before the yCollab bind are wiped by the bind's whole-doc replace.
    editableCompartment.of(EditorView.editable.of(false)),
    yjsCompartment.of([]),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...completionKeymap, indentWithTab]),
  ];
}
