/**
 * TDD for the image size writers — the pure alt-text helpers next to
 * parseSizeFromAlt and the one-transaction applyImageSize used by BOTH the
 * resize handle (ImageWidget) and the ImageToolbar:
 *   - setImageSize rewrites ONLY the `|…` width suffix: `300` ⇒ px, `50%` ⇒
 *     percent, `''` clears the suffix, an authored `x<height>` is preserved
 *     verbatim, a non-size `|` in the alt never splits the base
 *   - round-trips with parseSizeFromAlt in both directions
 *   - applyImageSize rewrites the embed in ONE transaction and re-selects the
 *     new node range (the toolbar stays up across its own edit)
 *
 * Harness: real EditorView + markdown parser + renderExtensions() in jsdom
 * (same pattern as atomic-delete-guard.test.ts).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { EditorState, EditorSelection } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { renderExtensions } from '../render-bundle';
import { transcludeMap } from './effects';
import {
  applyImageSuffix,
  ImageWidget,
} from './widgets';
import { runScopeHandlers } from '@codemirror/view';

const IMG = '![pic](ref:img1)';
const views: EditorView[] = [];

function makeView(doc: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc,
      extensions: [markdown({ base: markdownLanguage }), renderExtensions()],
    }),
    parent: document.createElement('div'),
  });
  views.push(view);
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  view.dispatch({ selection: { anchor: 0 } });
  return view;
}

beforeEach(() => {
  transcludeMap.set('img1', { kind: 'ref-image', title: 'Pic', imageUrl: 'http://localhost/x.png' });
});

afterEach(() => {
  transcludeMap.delete('img1');
  while (views.length) views.pop()!.destroy();
});

// ── applyImageSuffix: the resize handles' whole-suffix writer ────────────────

describe('applyImageSuffix', () => {
  it('corner write drops an authored height (proportional)', () => {
    const doc = '![pic|300x200](ref:img1)';
    const view = makeView(doc);
    expect(applyImageSuffix(view, 0, doc.length, '400')).toBe(true);
    expect(view.state.doc.toString()).toBe('![pic|400](ref:img1)');
  });

  it('edge write sets both axes over a percent width', () => {
    const doc = '![pic|20%](ref:img1)';
    const view = makeView(doc);
    expect(applyImageSuffix(view, 0, doc.length, '320x150')).toBe(true);
    expect(view.state.doc.toString()).toBe('![pic|320x150](ref:img1)');
  });

  it('rejects a non-size suffix without dispatching', () => {
    const view = makeView(IMG);
    expect(applyImageSuffix(view, 0, IMG.length, '1x')).toBe(false);
    expect(view.state.doc.toString()).toBe(IMG);
  });
});

// ── ImageWidget DOM: size on the wrapper, handles always mounted ─────────────

describe('ImageWidget DOM', () => {
  it('a percent width sizes the wrapper; the img fills it', () => {
    const view = makeView(IMG);
    const wrap = new ImageWidget('http://localhost/x.png', '20%').toDOM(view);
    expect(wrap.style.width).toBe('20%');
    expect((wrap.querySelector('img') as HTMLImageElement).style.width).toBe('100%');
  });

  it('mounts corner/corner-top/right/bottom handles even on a non-editable view', () => {
    const ro = new EditorView({
      state: EditorState.create({ doc: IMG, extensions: [EditorView.editable.of(false)] }),
      parent: document.createElement('div'),
    });
    views.push(ro);
    const wrap = new ImageWidget('http://localhost/x.png').toDOM(ro);
    const dirs = [...wrap.querySelectorAll('.cm-image-resize-handle')].map((h) => h.className);
    expect(dirs).toEqual([
      'cm-image-resize-handle cm-image-resize-corner',
      'cm-image-resize-handle cm-image-resize-corner-top',
      'cm-image-resize-handle cm-image-resize-right',
      'cm-image-resize-handle cm-image-resize-bottom',
    ]);
  });
});

// ── Enter on a selected image ────────────────────────────────────────────────

describe('Enter on a whole-selected image', () => {
  it('breaks the line after the image and keeps it', () => {
    const doc = `a ${IMG} b`;
    const view = makeView(doc);
    view.dispatch({ selection: EditorSelection.range(2, 2 + IMG.length) });
    const handled = runScopeHandlers(view, new KeyboardEvent('keydown', { key: 'Enter' }), 'editor');
    expect(handled).toBe(true);
    expect(view.state.doc.toString()).toBe(`a ${IMG}\n b`);
    expect(view.state.selection.main.empty).toBe(true);
    expect(view.state.selection.main.head).toBe(2 + IMG.length + 1);
  });

  it('a text selection is not handled (default Enter applies)', () => {
    const view = makeView(`ab ${IMG}`);
    view.dispatch({ selection: EditorSelection.range(0, 2) });
    expect(runScopeHandlers(view, new KeyboardEvent('keydown', { key: 'Enter' }), 'editor')).toBe(false);
  });
});
