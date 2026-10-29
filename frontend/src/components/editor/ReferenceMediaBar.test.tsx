/** ReferenceMediaBar — file actions live ON the media: the archive card, the
 * image overlay and the audio row each render MediaFileActions (download +
 * armed delete). A converted PDF (markdown ref + .pdf file_path) still renders
 * NOTHING here — its open-original action lives in ReferenceViewerBanner. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { Reference } from '../../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../api/client', () => ({ apiClient: { delete: vi.fn(), get: vi.fn(), post: vi.fn(), patch: vi.fn() } }));

import { ReferenceMediaBar } from './ReferenceMediaBar';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';

const NOW = '2026-01-01T00:00:00Z';

function baseRef(mediaType: Reference['media_type'], over: Partial<Reference> = {}): Reference {
  return {
    reference_id: 'ref-1',
    project_id: 'p',
    document_id: null,
    title: 'page.bin',
    media_type: mediaType,
    source_url: null,
    content: '',
    processing_status: 'ready',
    file_path: 'p/ref-1/page.bin',
    file_meta: { mime_type: 'application/octet-stream', file_size: 4096, original_name: 'page.bin' },
    created_at: NOW,
    updated_at: NOW,
    ...over,
  } as Reference;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

function renderBar(ref: Reference, opts: { canEdit?: boolean } = {}) {
  act(() => {
    root.render(<ReferenceMediaBar reference={ref} canEdit={opts.canEdit ?? true} />);
  });
  return container;
}
const deleteBtn = () => container.querySelector('button[title="deleteFile"]') as HTMLButtonElement | null;
const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

describe('MediaFileActions delete flow (real store)', () => {

  it('server deleted the text-less reference: it leaves the list and the viewer closes', async () => {
    const ref = baseRef('image');
    useAppStore.setState({ references: [ref], currentReference: ref, currentDocument: null });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue({ success: true, reference_deleted: true });
    renderBar(ref);
    click(deleteBtn()!);
    await act(async () => { click(deleteBtn()!); });
    const state = useAppStore.getState();
    expect(state.references.find(r => r.reference_id === 'ref-1')).toBeUndefined();
    expect(state.currentReference).toBeNull();
  });

  it('server kept the reference (it has text): it stays open as markdown', async () => {
    const ref = baseRef('image');
    useAppStore.setState({ references: [ref], currentReference: ref, currentDocument: null });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue({ success: true, reference_deleted: false });
    renderBar(ref);
    click(deleteBtn()!);
    await act(async () => { click(deleteBtn()!); });
    const state = useAppStore.getState();
    expect(state.references.find(r => r.reference_id === 'ref-1')?.media_type).toBe('markdown');
    expect(state.currentReference?.reference_id).toBe('ref-1');
    expect(state.currentReference?.media_type).toBe('markdown');
  });
});
