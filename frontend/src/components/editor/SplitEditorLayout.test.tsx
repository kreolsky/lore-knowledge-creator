/**
 * SplitEditorLayout — split-view audio player rendering.
 *
 * Pins the contract: in split mode the audio player bar (ReferenceMediaBar)
 * renders ABOVE the secondary editor in the RIGHT column only, and only for
 * audio and file (archive card) references. Markdown / image references and table-focus split render
 * no audio bar. Images keep their scroll-area preview (not doubled here).
 *
 * Stubbing strategy mirrors Editor.test.tsx: mock the heavy children (Editor,
 * ReferenceViewerBanner, TableFocusView/Banner) so no CM6/collab/table focus
 * view mounts, but keep ReferenceMediaBar REAL so the <audio> it emits (now via
 * the custom AudioPlayer — hidden media element, no native controls) is asserted.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let SplitEditorLayout: typeof import('./SplitEditorLayout').SplitEditorLayout;

beforeEach(async () => {
  vi.resetModules();

  // Heavy children → stubs. ReferenceMediaBar is intentionally NOT mocked.
  vi.doMock('../Editor', () => ({
    __esModule: true,
    Editor: () => createElement('div', { 'data-testid': 'editor-stub' }),
  }));
  const stub = () => null;
  vi.doMock('./ReferenceViewerBanner', () => ({ ReferenceViewerBanner: stub }));
  vi.doMock('./TableFocusView', () => ({
    TableFocusView: stub,
    TableFocusBanner: stub,
  }));

  // Minimal app-store slice (only the fields SplitEditorLayout reads).
  const appState: Record<string, unknown> = {
    accessLevel: 'full',
    setCurrentReference: () => {},
    setCurrentTable: () => {},
    currentTableLabel: 'Table One',
    documents: [],
    referenceSourceDocId: null,
  };
  const useAppStore: any = (selector: (s: any) => any) => selector(appState);
  useAppStore.subscribe = () => () => {};
  useAppStore.getState = () => appState;
  useAppStore.getInitialState = () => appState;
  vi.doMock('../../store/app-store', () => ({ useAppStore }));

  // Minimal ui-store slice (splitRatio + setter only).
  const uiState = { splitRatio: 0.5, setSplitRatio: () => {} };
  const useUIStore: any = (selector?: (s: any) => any) =>
    selector ? selector(uiState) : uiState;
  useUIStore.subscribe = () => () => {};
  useUIStore.getState = () => uiState;
  useUIStore.getInitialState = () => uiState;
  vi.doMock('../../store/ui-store', () => ({ useUIStore }));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  for (const m of [
    '../Editor', './ReferenceViewerBanner', './TableFocusView',
    '../../store/app-store', '../../store/ui-store',
  ]) {
    vi.doUnmock(m);
  }
});

const doc = { document_id: 'd1', title: 'Doc', type: 'doc' } as any;

function makeRef(media_type: 'markdown' | 'audio' | 'image' | 'file', file_path?: string) {
  return {
    reference_id: 'r1',
    project_id: 'p1',
    document_id: null,
    title: 'Ref',
    media_type,
    source_url: null,
    content: media_type === 'markdown' ? '# hi' : undefined,
    file_path,
  } as any;
}

describe('SplitEditorLayout audio player', () => {
  it('renders <audio> above the secondary editor for an audio reference with file_path', async () => {
    SplitEditorLayout = (await import('./SplitEditorLayout')).SplitEditorLayout;
    act(() => root.render(createElement(SplitEditorLayout, {
      document: doc,
      reference: makeRef('audio', 'audio/interview.mp3'),
    })));

    const audios = container.querySelectorAll('audio');
    expect(audios.length).toBe(1);

    // The audio sits before the secondary editor stub (player pinned above text).
    const stubs = container.querySelectorAll('[data-testid="editor-stub"]');
    expect(stubs.length).toBe(2); // doc column + reference column
    const following = audios[0].compareDocumentPosition(stubs[1]) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(following).toBeTruthy();
  });

  it('renders no <audio> for a markdown reference', async () => {
    SplitEditorLayout = (await import('./SplitEditorLayout')).SplitEditorLayout;
    act(() => root.render(createElement(SplitEditorLayout, {
      document: doc,
      reference: makeRef('markdown'),
    })));

    expect(container.querySelectorAll('audio').length).toBe(0);
  });

  it('renders no <audio> for an image reference (image preview stays in the editor)', async () => {
    SplitEditorLayout = (await import('./SplitEditorLayout')).SplitEditorLayout;
    act(() => root.render(createElement(SplitEditorLayout, {
      document: doc,
      reference: makeRef('image', 'img/pic.png'),
    })));

    expect(container.querySelectorAll('audio').length).toBe(0);
  });

  it('renders the archive card (download + delete) above the secondary editor for a file reference', async () => {
    SplitEditorLayout = (await import('./SplitEditorLayout')).SplitEditorLayout;
    act(() => root.render(createElement(SplitEditorLayout, {
      document: doc,
      reference: makeRef('file', 'p1/r1/page.zip'),
    })));

    // The header menu no longer carries the original for a file ref — the card is
    // the only place its file can be downloaded or dropped.
    const download = container.querySelector('a[download]');
    expect(download).not.toBeNull();
    expect(container.querySelectorAll('button').length).toBe(1);
    const stubs = container.querySelectorAll('[data-testid="editor-stub"]');
    expect(download!.compareDocumentPosition(stubs[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders no <audio> for a table split (reference undefined)', async () => {
    SplitEditorLayout = (await import('./SplitEditorLayout')).SplitEditorLayout;
    act(() => root.render(createElement(SplitEditorLayout, {
      document: doc,
      table: { document_id: 'd1', table_id: 't1' } as any,
    })));

    expect(container.querySelectorAll('audio').length).toBe(0);
  });
});
