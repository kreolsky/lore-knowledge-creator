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
  it('a pdf goes to addFile when the surface takes files', () => {
    const addFile = vi.fn();
    const addImage = vi.fn();
    const pdf = new File(['%PDF'], 'a.pdf', { type: 'application/pdf' });
    drop(mount({ addFile, addImage }), [pdf]);
    expect(addFile).toHaveBeenCalledWith(pdf);
    expect(addImage).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it('without addFile (notes chat) a pdf is refused with a toast, nothing queued', () => {
    const addImage = vi.fn();
    drop(mount({ addImage }), [new File(['%PDF'], 'a.pdf', { type: 'application/pdf' })]);
    expect(addImage).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('chatFileImagesOnly', 'error');
  });

  it('a rar is refused before any upload', () => {
    const addFile = vi.fn();
    drop(mount({ addFile }), [new File(['x'], 'a.rar', { type: 'application/vnd.rar' })]);
    expect(addFile).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('chatFileTypeRefused', 'error');
  });

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

  it('a file over 100 MB is refused before any upload', () => {
    const addFile = vi.fn();
    const big = new File(['x'], 'big.zip', { type: 'application/zip' });
    Object.defineProperty(big, 'size', { value: 100 * 1024 * 1024 + 1 });
    drop(mount({ addFile }), [big]);
    expect(addFile).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('chatFileTooLarge', 'error');
  });
});
