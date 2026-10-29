/**
 * Tests for atomic blocks — image embeds (`![alt](ref:id)`) and table-block anchors
 * (`![label](table:id)`) behave as whole objects in the DOCUMENT editor stack
 * (`renderExtensions()`):
 *   - the widget renders under EVERY caret position (raw markdown never revealed)
 *   - plain motion jumps over the range as one unit; Shift+Left selects it whole
 *   - a one-key delete (Backspace / Delete / Alt-Backspace / Mod-Backspace) with an
 *     EMPTY selection is swallowed. Pinned user rule: «мы не удаляем таблицу/картинку
 *     одним символом — только выделив её целиком»
 *   - a delete over a selection that covers the block whole still removes it
 *   - a line join into/out of a table anchor line is swallowed (whole-line shape)
 *   - checkbox deletes are NOT guarded (unchanged contract)
 *   - remote/programmatic deletes carry no delete user event and pass
 *   - a plain press on the image selects exactly its node range
 *   - a selection covering the block whole carries the cm-atomic-selected mark
 *
 * Harness: real EditorView + markdown parser + renderExtensions() in jsdom (same
 * pattern as table-block-field.test.ts / live-preview-plugin.test.ts).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { renderExtensions } from '../render-bundle';
import { transcludeMap } from './effects';

const IMG = '![pic](ref:img1)';
const views: EditorView[] = [];

function makeView(doc: string, caret: number): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc,
      selection: { anchor: caret },
      extensions: [markdown({ base: markdownLanguage }), renderExtensions()],
    }),
    parent: document.createElement('div'),
  });
  views.push(view);
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  // Force a decoration rebuild now that the (async) parse has completed.
  view.dispatch({ selection: { anchor: caret } });
  return view;
}

beforeEach(() => {
  transcludeMap.set('img1', { kind: 'ref-image', title: 'Pic', imageUrl: 'http://localhost/x.png' });
});

afterEach(() => {
  transcludeMap.delete('img1');
  while (views.length) views.pop()!.destroy();
});

// ── Click on the image ────────────────────────────────────────────────────────

describe('atomic image — click', () => {

  // WHY: two embeds sharing a boundary (`![a]…![b]…`) — the second's start IS the
  // first's end; resolving it to the first makes a resize write the suffix into the
  // OTHER image. No drive clicks adjacent images.
  it('mousedown on the SECOND of two touching images selects the second', () => {
    const IMG2 = '![pic2](ref:img2)';
    transcludeMap.set('img2', { kind: 'ref-image', title: 'Pic2', imageUrl: 'http://localhost/y.png' });
    try {
      const doc = `a\n${IMG}${IMG2}\nb`;
      const view = makeView(doc, 0);
      const imgs = view.contentDOM.querySelectorAll('img:not(.cm-widgetBuffer)');
      expect(imgs.length).toBe(2);
      imgs[1].dispatchEvent(
        new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, detail: 1, clientX: 10, clientY: 10 }),
      );
      const main = view.state.selection.main;
      expect(view.state.sliceDoc(main.from, main.to)).toBe(IMG2);
    } finally {
      transcludeMap.delete('img2');
    }
  });
});
