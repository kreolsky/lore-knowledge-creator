/**
 * Regression: TOC 'scroll-to-line' must reach the PUBLIC viewer's CM6 instance.
 *
 * HISTORY: the ONLY 'scroll-to-line' subscriber was useEditorEvents (authed
 * Editor), so on /s/:token clicking a TOC entry emitted into an empty bus and
 * did nothing. PublicSharePage re-wired navigate-to-document/reference with
 * public-safe handlers but scroll-to-line was missed. The fix mounts
 * useScrollToLine in PublicEditor; this spec pins it against the real
 * component + real CodeMirrorEditor (not a stubbed CM6).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EditorView } from '@codemirror/view';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DOC = '# Title\n\npara one\n\n## Section Two\n\nbody\n\n### Deep Three\n';
const HEADINGS = [
  { line: 1, level: 1, text: 'Title' },
  { line: 4, level: 2, text: 'Section Two' },
  { line: 7, level: 3, text: 'Deep Three' },
];

let container: HTMLDivElement;
let root: Root;
let PublicEditor: typeof import('./PublicEditor').PublicEditor;
let emit: typeof import('../events/event-bus').emit;

beforeEach(async () => {
  vi.resetModules();

  // Per existing mock conventions (DocumentTree.test.tsx): selector-style
  // store seeded with a real currentDocument (multi-heading + headings).
  const appState = {
    currentDocument: {
      document_id: 'doc-1',
      content: DOC,
      headings: HEADINGS,
      tables_json: null,
    },
    currentReference: null,
    setCurrentReference: () => {},
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: (selector: (s: typeof appState) => unknown) => selector(appState),
  }));

  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: (k: string) => k }),
  }));
  // Public-surface hooks not under test: seeding transcludeMap / arrow nav.
  vi.doMock('../hooks/usePublicTransclusionSync', () => ({
    usePublicTransclusionSync: () => {},
  }));
  vi.doMock('../hooks/useImageReferenceNav', () => ({
    useImageReferenceNav: () => {},
  }));

  // REAL event bus: the emit below must reach the hook mounted by PublicEditor.
  emit = (await import('../events/event-bus')).emit;
  PublicEditor = (await import('./PublicEditor')).PublicEditor;

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../i18n');
  vi.doUnmock('../hooks/usePublicTransclusionSync');
  vi.doUnmock('../hooks/useImageReferenceNav');
});

describe('PublicEditor — TOC scroll-to-line reaches the public CM6 view', () => {
  it('emitting scroll-to-line dispatches scrollIntoView for that heading line', { timeout: 30_000 }, async () => {
    act(() => root.render(createElement(PublicEditor)));

    const cmHost = container.querySelector('.cm-editor');
    expect(cmHost, 'real CodeMirrorEditor must be mounted').not.toBeNull();
    // 6.39 exposes findFromDOM (EditorView.find was removed in this line).
    const view = EditorView.findFromDOM(cmHost as HTMLElement);
    expect(view).toBeInstanceOf(EditorView);

    const dispatchSpy = vi.spyOn(view!, 'dispatch');
    act(() => emit('scroll-to-line', { line: 4 }));

    expect(dispatchSpy).toHaveBeenCalledOnce();
    const effects = dispatchSpy.mock.calls[0][0].effects;
    const effect = Array.isArray(effects) ? effects[0] : effects;
    expect(effect!.value.range.from).toBe(view!.state.doc.line(4).from);
    expect(effect!.value.y).toBe('start');
    expect(effect!.value.yMargin).toBe(60);
  });
});
