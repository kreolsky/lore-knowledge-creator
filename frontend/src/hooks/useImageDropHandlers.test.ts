/** useImageDropHandlers — a non-image drop goes to `addFile` where the surface
 * takes files (AI chat, full access) and is refused with a toast everywhere
 * else (notes chat), never skipped silently. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement, act, type DragEvent } from 'react';
import { createRoot, type Root } from 'react-dom/client';

const showToast = vi.fn();
vi.mock('../store/app-store', () => ({
  useAppStore: { getState: () => ({ showToast, maxAttachmentMb: 5 }) },
}));
vi.mock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { useImageDropHandlers } from './useImageDropHandlers';

type Handlers = ReturnType<typeof useImageDropHandlers>['dragHandlers'];
let container: HTMLDivElement;
let root: Root;

function mount(opts: { addFile?: (f: File) => void; addImage?: (u: string) => void }): Handlers {
  let handlers!: Handlers;
  function Probe() {
    handlers = useImageDropHandlers({
      getPending: () => [], addImage: opts.addImage ?? vi.fn(), addFile: opts.addFile, enabled: true,
    }).dragHandlers;
    return null;
  }
  act(() => root.render(createElement(Probe)));
  return handlers;
}

function drop(handlers: Handlers, files: File[]) {
  act(() => handlers.onDrop({
    preventDefault: () => {}, stopPropagation: () => {}, dataTransfer: { files },
  } as unknown as DragEvent));
}

beforeEach(() => {
  vi.clearAllMocks();
  container = document.createElement('div');
  root = createRoot(container);
});
afterEach(() => act(() => root.unmount()));

describe('useImageDropHandlers — non-image files', () => {

  it.each([
    ['clip.mp4', 'video/mp4'],
    ['memo.webm', 'video/webm'],
  ])('%s (video) goes to addFile — only the size bounds it', (name, type) => {
    const addFile = vi.fn();
    const video = new File(['x'], name, { type });
    drop(mount({ addFile }), [video]);
    expect(addFile).toHaveBeenCalledWith(video);
    expect(showToast).not.toHaveBeenCalled();
  });
});
