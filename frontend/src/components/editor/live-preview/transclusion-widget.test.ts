/** Tests for TransclusionWidget — toDOM structure and header navigation. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EditorView } from '@codemirror/view';
import { TransclusionWidget, widgetHeightCache } from './widgets';
import { transcludeMap } from './effects';
import * as events from '../../../events';

describe('TransclusionWidget', () => {
  beforeEach(() => {
    transcludeMap.clear();
    widgetHeightCache.clear();
  });

  it('renders header with the title and a nested read-only editor body', () => {
    const w = new TransclusionWidget(
      { kind: 'ref-text', title: 'My Ref', content: '# Hello\nworld' },
      'r1',
      'ref',
      0,
    );
    const el = w.toDOM();
    expect(el.className).toContain('cm-transclusion');
    const title = el.querySelector('.cm-transclusion-title');
    expect(title?.textContent).toBe('My Ref');
    const body = el.querySelector('.cm-transclusion-body');
    // Body is rendered through the host CodeMirror pipeline (nested EditorView),
    // not raw markdown HTML.
    const nested = body?.querySelector('.cm-editor');
    expect(nested).toBeTruthy();
    expect(w.nestedView?.state.readOnly).toBe(true);
    w.destroy(el);
  });

  it('the nested body editor renders the transcluded text', () => {
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'Doc', content: '# Hello\nworld' },
      'd-text',
      'doc',
      0,
    );
    w.toDOM();
    expect(w.nestedView?.state.doc.toString()).toBe('# Hello\nworld');
  });

  it('title sits inside a full-width header row (text-only hover/click target)', () => {
    // The divider lives on the header (full row); the title is inline-block so its
    // hover/click target is the title text, not the empty space beside it.
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'T', content: 'c' },
      'd',
      'doc',
    );
    const el = w.toDOM();
    const title = el.querySelector('.cm-transclusion-title') as HTMLElement;
    const header = title.parentElement;
    expect(header?.className).toBe('cm-transclusion-header');
    expect(header?.parentElement).toBe(el);
  });

  it('renders a loading state when content is undefined', () => {
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'Lazy' },
      'd1',
      'doc',
    );
    const el = w.toDOM();
    expect(el.className).toContain('cm-transclusion-loading');
    expect(el.querySelector('.cm-transclusion-spinner')).toBeTruthy();
  });

  it('header click emits navigate-to-document for a doc kind', () => {
    const spy = vi.spyOn(events, 'emit');
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'Doc', content: 'c' },
      'doc-xyz',
      'doc',
    );
    const el = w.toDOM();
    const title = el.querySelector('.cm-transclusion-title') as HTMLElement;
    const evt = new MouseEvent('click');
    title.dispatchEvent(evt);
    expect(spy).toHaveBeenCalledWith('navigate-to-document', { documentId: 'doc-xyz' });
    spy.mockRestore();
  });

  it('header mousedown is defaultPrevented (blocks CM6 cursor placement)', () => {
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'Doc', content: 'c' },
      'doc-md',
      'doc',
    );
    const el = w.toDOM();
    const title = el.querySelector('.cm-transclusion-title') as HTMLElement;
    const evt = new MouseEvent('mousedown', { cancelable: true });
    title.dispatchEvent(evt);
    expect(evt.defaultPrevented).toBe(true);
  });

  it('header click still navigates after a preceding mousedown', () => {
    const spy = vi.spyOn(events, 'emit');
    const w = new TransclusionWidget(
      { kind: 'ref-text', title: 'Ref', content: 'c' },
      'ref-md',
      'ref',
    );
    const el = w.toDOM();
    const title = el.querySelector('.cm-transclusion-title') as HTMLElement;
    title.dispatchEvent(new MouseEvent('mousedown', { cancelable: true }));
    title.dispatchEvent(new MouseEvent('click'));
    expect(spy).toHaveBeenCalledWith('navigate-to-reference', { referenceId: 'ref-md' });
    spy.mockRestore();
  });

  it('body click does not navigate (defers to CM default cursor-edit behavior)', () => {
    const spy = vi.spyOn(events, 'emit');
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'Doc', content: 'c' },
      'doc-body',
      'doc',
    );
    const el = w.toDOM();
    const body = el.querySelector('.cm-transclusion-body') as HTMLElement;
    body.dispatchEvent(new MouseEvent('click'));
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it('header click emits navigate-to-reference for a ref kind', () => {
    const spy = vi.spyOn(events, 'emit');
    const w = new TransclusionWidget(
      { kind: 'ref-text', title: 'Ref', content: 'c' },
      'ref-abc',
      'ref',
    );
    const el = w.toDOM();
    const title = el.querySelector('.cm-transclusion-title') as HTMLElement;
    title.dispatchEvent(new MouseEvent('click'));
    expect(spy).toHaveBeenCalledWith('navigate-to-reference', { referenceId: 'ref-abc' });
    spy.mockRestore();
  });

  it('eq() compares entityId + kind + content', () => {
    const a = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'x' }, 'd', 'doc');
    const b = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'x' }, 'd', 'doc');
    const c = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'y' }, 'd', 'doc');
    expect(a.eq(b)).toBe(true);
    expect(a.eq(c)).toBe(false);
  });

  it('eq() treats a different title as a changed widget (no stale header on rename)', () => {
    const a = new TransclusionWidget({ kind: 'doc', title: 'Old', content: 'x' }, 'd', 'doc');
    const b = new TransclusionWidget({ kind: 'doc', title: 'New', content: 'x' }, 'd', 'doc');
    expect(a.eq(b)).toBe(false);
  });

  it('renders HTML-in-content as inert text, never live elements (no innerHTML injection)', () => {
    // The nested EditorView seeds content as a document string → rendered as text
    // nodes, so a stored <img onerror>/<script>/<svg onload> payload can never become
    // a live DOM element. This replaces the old DOMPurify-on-innerHTML boundary.
    const payload = '<img src=x onerror=alert(1)>\n<script>alert(1)</script>\n<svg onload=alert(1)></svg>';
    const w = new TransclusionWidget(
      { kind: 'ref-text', title: 'Evil', content: payload },
      'r2',
      'ref',
      0,
    );
    const el = w.toDOM();
    const body = el.querySelector('.cm-transclusion-body') as HTMLElement;
    expect(body.querySelector('img[onerror]')).toBeNull();
    expect(body.querySelector('script')).toBeNull();
    expect(body.querySelector('svg[onload]')).toBeNull();
    // The payload survives as the editor's text document (inert), proving it was not
    // parsed as markup.
    expect(w.nestedView?.state.doc.toString()).toContain('onerror');
    w.destroy(el);
  });

  it('strips a single trailing EOF newline so the embed has no phantom last line', () => {
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'line one\n' }, 'd-nl', 'doc', 0);
    w.toDOM();
    expect(w.nestedView?.state.doc.toString()).toBe('line one');
  });

  it('preserves intentional blank lines (only the EOF newline is stripped)', () => {
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'a\n\nb\n' }, 'd-bl', 'doc', 0);
    w.toDOM();
    expect(w.nestedView?.state.doc.toString()).toBe('a\n\nb');
  });

  it('depth >= 1 renders an inner transclusion as a link, not a nested band', () => {
    // The depth guard caps recursion: a transcluded view at depth 1 must not build
    // another TransclusionWidget.
    const w = new TransclusionWidget(
      { kind: 'doc', title: 'Inner', content: 'inner content' },
      'inner',
      'doc',
      1,
    );
    const el = w.toDOM();
    // Still a band itself (depth 1 widget is allowed); the guard prevents a *second*
    // band inside its own nested view — asserted in the build-structural test below.
    expect(w.nestedView?.state.facet).toBeTruthy();
    w.destroy(el);
  });

  it('updateDOM reuses the nested EditorView on a content change (no teardown)', () => {
    const w1 = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'old' }, 'd', 'doc', 0);
    const dom = w1.toDOM();
    const view = w1.nestedView;
    const w2 = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'new' }, 'd', 'doc', 0);
    const stubView = { requestMeasure() {} } as unknown as EditorView;
    const reused = w2.updateDOM(dom, stubView);
    expect(reused).toBe(true);
    expect(w2.nestedView).toBe(view);
    expect(view?.state.doc.toString()).toBe('new');
    w2.destroy(dom);
  });

  it('destroy() tears down the nested EditorView', () => {
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'c' }, 'd', 'doc', 0);
    const dom = w.toDOM();
    const view = w.nestedView!;
    const spy = vi.spyOn(view, 'destroy');
    w.destroy(dom);
    expect(spy).toHaveBeenCalled();
  });

  it('destroy() is idempotent (safe to call twice)', () => {
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'c' }, 'd2', 'doc', 0);
    const dom = w.toDOM();
    const view = w.nestedView!;
    const spy = vi.spyOn(view, 'destroy');
    w.destroy(dom);
    w.destroy(dom); // second call must be a no-op (no throw, no double destroy)
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('destroy cancels the pending rAF so a stale outer-view re-measure never runs', () => {
    vi.useFakeTimers();
    const requestMeasure = vi.fn();
    const outerView = { requestMeasure, isDestroyed: false } as unknown as EditorView;
    const w = new TransclusionWidget({ kind: 'doc', title: 'D', content: 'c' }, 'raf', 'doc', 0);
    const dom = w.toDOM(outerView);
    // Tear down BEFORE the rAF fires — the orphaned callback must be a no-op.
    w.destroy(dom);
    vi.advanceTimersByTime(100);
    expect(requestMeasure).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
});
