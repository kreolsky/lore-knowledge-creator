/** Lazy cell placeholder renders exactly what the mounted cell editor renders. */
// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { renderCellStatic, renderCellThroughEditor, isPlainCellText } from './cell-static-render';
import { editableCellExtensions } from '../render-bundle';
import { revealAtCursor } from './effects';

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

/** The unfocused mounted cell editor, configured as table-block-widget mounts it. */
function mountedCellText(text: string): string {
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  const view = new EditorView({
    parent,
    state: EditorState.create({ doc: text, extensions: [editableCellExtensions(), revealAtCursor.of(false)] }),
  });
  const out = view.contentDOM.textContent ?? '';
  view.destroy();
  parent.remove();
  return out;
}

function placeholderText(text: string): string {
  return renderCellStatic(text).textContent ?? '';
}

const CASES = [
  '[tldraw](https://github.com/tldraw/tldraw)',
  '**bold** and *em*',
  '[[Some Doc]]',
  'a https://example.com b',
  '`code` -> arrow',
];

describe('renderCellStatic', () => {
  it.each(CASES)('matches the mounted cell editor for %s', (text) => {
    expect(placeholderText(text)).toBe(mountedCellText(text));
  });

  it('collapses a markdown link to its label', () => {
    expect(placeholderText('[tldraw](https://github.com/tldraw/tldraw)')).toBe('tldraw');
  });

  it('a markdown cell copies line content only — no editor wrappers', () => {
    const frag = renderCellThroughEditor('**bold** [x](https://e.com)');
    const holder = document.createElement('div');
    holder.appendChild(frag);
    expect(holder.querySelector('.cm-editor, .cm-scroller, .cm-content, .cm-line')).toBeNull();
    expect(holder.children.length).toBe(1);
    expect(holder.querySelector('.cm-strong')?.textContent).toBe('bold');
  });

  it('keeps one block per line in a multi-line cell', () => {
    const holder = document.createElement('div');
    holder.appendChild(renderCellThroughEditor('**a**\nb'));
    expect([...holder.children].map((c) => c.textContent)).toEqual(['a', 'b']);
  });

  it('renders each call independently (shared renderer is reset per cell)', () => {
    placeholderText('first **cell**');
    expect(placeholderText('second')).toBe('second');
  });

  const PLAIN = ['42', '7.5', '100%', 'Можно деплоить', 'Hello, world?', '«цитата» №3'];

  it.each(PLAIN)('plain text is a bare text node with the full render\'s text: %s', (text) => {
    expect(isPlainCellText(text)).toBe(true);
    const node = renderCellStatic(text);
    expect(node.nodeType).toBe(Node.TEXT_NODE);
    expect(node.textContent).toBe(renderCellThroughEditor(text).textContent);
    expect(node.textContent).toBe(mountedCellText(text));
  });

  const NOT_PLAIN = ['', '1. item', '1.', '    indented', 'a -> b', 'ab--cd', '(c) 2026', '[x](y)', 'https://e.com', '**b**', '~50K', '#tag'];

  it.each(NOT_PLAIN)('markdown-capable text takes the full render: %s', (text) => {
    expect(isPlainCellText(text)).toBe(false);
  });
});
