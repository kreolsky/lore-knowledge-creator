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

function pdfRef(over: Partial<Reference> = {}): Reference {
  return baseRef('markdown', {
    title: 'paper.pdf',
    file_path: 'p/ref-1/paper.pdf',
    file_meta: { mime_type: 'application/pdf', file_size: 4096, original_name: 'paper.pdf' },
    ...over,
  });
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

const downloadLink = () => container.querySelector('a[aria-label="download"]') as HTMLAnchorElement | null;
const deleteBtn = () => container.querySelector('button[title="deleteFile"]') as HTMLButtonElement | null;
const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

describe('ReferenceMediaBar PDF branch', () => {
  it('renders nothing for a markdown ref with a .pdf file_path (the action moved to the banner)', () => {
    const el = renderBar(pdfRef());
    expect(el.querySelector('a')).toBeNull();
    expect(el.textContent).toBe('');
  });

  it('renders nothing for a markdown ref with a non-pdf file_path (docx has no card either)', () => {
    const el = renderBar(pdfRef({
      title: 'paper.docx',
      file_path: 'p/ref-1/paper.docx',
      file_meta: { mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', file_size: 4096, original_name: 'paper.docx' },
    }));
    expect(el.querySelector('a')).toBeNull();
  });
});

describe.each(['image', 'file'] as const)('ReferenceMediaBar %s branch', (mediaType) => {
  it('canEdit: download link + delete button', () => {
    renderBar(baseRef(mediaType));
    expect(downloadLink()).not.toBeNull();
    expect(deleteBtn()).not.toBeNull();
  });

  it('canEdit=false (public share / viewer): download only, no delete', () => {
    renderBar(baseRef(mediaType), { canEdit: false });
    expect(downloadLink()).not.toBeNull();
    expect(deleteBtn()).toBeNull();
  });

  it('delete hidden while processing_status is uploading/processing (job running on the file)', () => {
    renderBar(baseRef(mediaType, { processing_status: 'processing' }));
    expect(downloadLink()).not.toBeNull();
    expect(deleteBtn()).toBeNull();
    renderBar(baseRef(mediaType, { processing_status: 'uploading' }));
    expect(deleteBtn()).toBeNull();
  });
});

describe('ReferenceMediaBar audio branch', () => {
  it('canEdit: player download link + delete rendered right after it', () => {
    renderBar(baseRef('audio'));
    const del = deleteBtn();
    expect(del).not.toBeNull();
    // showDownload=false on the audio row — the PLAYER's download is the one link.
    expect(container.querySelectorAll('a[aria-label="download"]').length).toBe(1);
    expect(del!.previousElementSibling).toBe(downloadLink());
  });

  it('canEdit=false: download only', () => {
    renderBar(baseRef('audio'), { canEdit: false });
    expect(downloadLink()).not.toBeNull();
    expect(deleteBtn()).toBeNull();
  });
});

describe('MediaFileActions delete flow (real store)', () => {
  it('first click arms, second click DELETEs /references/{id}/file', async () => {
    const ref = baseRef('image');
    useAppStore.setState({ references: [ref], currentReference: ref, currentDocument: null });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue({ success: true });
    renderBar(ref);
    click(deleteBtn()!);
    expect(apiClient.delete).not.toHaveBeenCalled();
    await act(async () => { click(deleteBtn()!); });
    expect(apiClient.delete).toHaveBeenCalledWith('/references/ref-1/file');
  });

  it('API failure: store rolled back and error toast shown (no silent degradation)', async () => {
    const ref = baseRef('image');
    useAppStore.setState({ references: [ref], currentReference: ref, currentDocument: null });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    renderBar(ref);
    click(deleteBtn()!);
    await act(async () => { click(deleteBtn()!); });
    await act(async () => { await Promise.resolve(); });
    const state = useAppStore.getState();
    expect(state.references.find(r => r.reference_id === 'ref-1')?.file_path).toBe(ref.file_path);
    expect(state.references.find(r => r.reference_id === 'ref-1')?.media_type).toBe('image');
    expect(state.currentReference?.file_path).toBe(ref.file_path);
    expect(state.currentReference?.media_type).toBe('image');
    expect(state.toast?.message).toBe('failedToDeleteReferenceFile');
    expect(state.toast?.type).toBe('error');
  });
});
