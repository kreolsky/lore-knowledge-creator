/** Tests for ImageGallery staged-delete (archive/restore). */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import type { Reference } from '../../types';
import { ImageGallery } from './ImageGallery';

vi.mock('./ref-utils', () => ({ hueForId: () => 0 }));
vi.mock('../ui', () => ({
  IconButton: (props: { title?: string; onClick?: (e: any) => void; children?: any }) => (
    <button data-title={props.title} onClick={props.onClick}>{props.children}</button>
  ),
}));
vi.mock('../../editor/active-editor', () => ({ useEditorView: () => () => null }));
vi.mock('../../hooks/useArmedAction', () => ({
  // Two-click arming so the archived (stage-2) delete path can be asserted.
  useArmedAction: () => {
    let armed = false;
    let pending: (() => void) | null = null;
    return {
      get armed() { return armed; },
      arm: () => { armed = true; },
      disarm: () => { armed = false; pending = null; },
      handleClick: (cb: () => void) => {
        if (armed && pending) { const p = pending; pending = null; armed = false; p(); }
        else { armed = true; pending = cb; }
      },
    };
  },
}));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

function imgRef(over: Partial<Reference>): Reference {
  return {
    reference_id: 'img1',
    project_id: 'p',
    document_id: 'd',
    title: 'Img',
    media_type: 'image',
    source_url: null,
    file_path: '/x.png',
    file_meta: null,
    processing_status: null,
    ...over,
  } as Reference;
}

function renderGallery(refs: Reference[], handlers: Record<string, any>) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <ImageGallery
        references={refs}
        activeRefId={null}
        canEdit={true}
        getLabel={() => null}
        onSelect={() => {}}
        onDelete={() => {}}
        onArchive={() => {}}
        onRestore={() => {}}
        onChangeParent={() => {}}
        {...handlers}
      />,
    );
  });
  return { container, root };
}

describe('ImageGallery staged-delete (archive/restore)', () => {
  beforeEach(() => { document.body.innerHTML = ''; });

  it('live thumb: trash is a single-click archive', () => {
    const onArchive = vi.fn();
    const { container, root } = renderGallery([imgRef({})], { onArchive });
    const trash = container.querySelector('[data-title="archiveReference"]') as HTMLButtonElement;
    expect(trash).toBeTruthy();
    act(() => { trash.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onArchive).toHaveBeenCalledTimes(1);
    root.unmount();
  });

  it('archived thumb: shows Restore + armed-delete trash', () => {
    const onRestore = vi.fn();
    const onDelete = vi.fn();
    const { container, root } = renderGallery(
      [imgRef({ archived: true })], { onRestore, onDelete },
    );
    const restore = container.querySelector('[data-title="restoreReference"]') as HTMLButtonElement;
    const trash = container.querySelector('[data-title="deleteReferencePermanently"]') as HTMLButtonElement;
    expect(restore).toBeTruthy();
    expect(trash).toBeTruthy();
    expect(container.querySelector('[data-title="archiveReference"]')).toBeNull();
    // Restore is single-click.
    act(() => { restore.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onRestore).toHaveBeenCalledTimes(1);
    // Trash arms on first click, fires on second.
    act(() => { trash.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onDelete).not.toHaveBeenCalled();
    act(() => { trash.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onDelete).toHaveBeenCalledTimes(1);
    root.unmount();
  });
});
