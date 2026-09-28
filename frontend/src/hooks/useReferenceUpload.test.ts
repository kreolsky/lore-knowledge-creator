/**
 * Regression test for the public-share polling guard in useReferenceUpload.
 *
 * Context: on the anonymous /s/:token surface, ReferencesPanel still mounts
 * useReferenceUpload (it owns drag/drop + upload handlers). The hook's
 * transcription-status polling effect used to fire unconditionally, hitting the
 * authed /references/{id}/status endpoint → 401 → apiClient redirects to '/' and
 * kicks the visitor off the share page after ~3s. The effect must early-return
 * whenever isPublicShare is true (mirrors every other public-share guard).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

import type { Reference } from '../types';

const makeProcessingRef = (id: string): Reference => ({
  reference_id: id,
  project_id: 'p',
  document_id: 'd',
  title: `ref-${id}`,
  media_type: 'audio',
  source_url: '',
  content: '',
  // A mid-processing reference arms the poll — exactly the subtree-share scenario.
  processing_status: 'processing',
  file_path: null,
  file_meta: null,
  headings: [],
  created_at: '',
  updated_at: '',
});

// Module-level store snapshots — the doMock factories read these at call (render)
// time, so each test can mutate them before rendering the harness.
let __refs: Reference[] = [];
let __isPublicShare = false;
let __project: { project_id: string } | null = null;
let __document: { document_id: string } | null = null;

let container: HTMLDivElement;
let root: Root;
let getMock: ReturnType<typeof vi.fn>;
let uploadMock: ReturnType<typeof vi.fn>;
let toastMock: ReturnType<typeof vi.fn>;
let hook: ReturnType<typeof import('./useReferenceUpload').useReferenceUpload> | null = null;
let useReferenceUpload: typeof import('./useReferenceUpload').useReferenceUpload;

function Harness() {
  hook = useReferenceUpload();
  return null;
}

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  __refs = [];
  __isPublicShare = false;
  __project = null;
  __document = null;

  getMock = vi.fn().mockResolvedValue({ processing_status: 'ready' });
  uploadMock = vi.fn();
  toastMock = vi.fn();
  hook = null;

  vi.doMock('../api/client', () => ({
    apiClient: { get: getMock, upload: uploadMock },
  }));

  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: (k: string) => k }),
  }));

  vi.doMock('../store/app-store', () => {
    const useAppStore: any = (selector: (s: any) => any) =>
      selector({
        currentProject: __project,
        currentDocument: __document,
        references: __refs,
        addReference: () => {},
        updateReference: () => {},
        replaceReference: () => {},
        addPendingUploadRefIds: () => {},
        removePendingUploadRefIds: () => {},
      });
    useAppStore.getState = () => ({ showToast: toastMock });
    useAppStore.setState = () => {};
    return { useAppStore };
  });

  vi.doMock('../store/ui-store', () => {
    const useUIStore: any = (selector?: (s: any) => any) =>
      selector ? selector({ isPublicShare: __isPublicShare }) : { isPublicShare: __isPublicShare };
    useUIStore.getState = () => ({ isPublicShare: __isPublicShare });
    useUIStore.setState = () => {};
    return { useUIStore };
  });

  const mod = await import('./useReferenceUpload');
  useReferenceUpload = mod.useReferenceUpload;

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.doUnmock('../api/client');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
});

const statusCallsFor = (id: string) =>
  getMock.mock.calls.filter(([url]) => String(url).includes(`/references/${id}/status`));

describe('useReferenceUpload polling — public-share guard', () => {
  it('regression: does NOT poll /references/:id/status when isPublicShare is true', async () => {
    __refs = [makeProcessingRef('r1')];
    __isPublicShare = true;

    act(() => root.render(createElement(Harness)));

    // Initial poll is scheduled at t=3000ms; advancing past it must not fire it.
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });

    expect(statusCallsFor('r1')).toHaveLength(0);
  });

  it('control: DOES poll /references/:id/status when isPublicShare is false', async () => {
    __refs = [makeProcessingRef('r2')];
    __isPublicShare = false;

    act(() => root.render(createElement(Harness)));

    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });

    expect(statusCallsFor('r2').length).toBeGreaterThanOrEqual(1);
  });

  it('stops polling once isPublicShare flips to true mid-session', async () => {
    __refs = [makeProcessingRef('r3')];
    __isPublicShare = false;

    act(() => root.render(createElement(Harness)));

    // First poll fires at t=3000.
    await act(async () => {
      vi.advanceTimersByTime(3000);
      await Promise.resolve();
    });
    const callsBefore = statusCallsFor('r3').length;
    expect(callsBefore).toBeGreaterThanOrEqual(1);

    // Flip to public share — the guard must re-run and tear down the timer.
    __isPublicShare = true;
    act(() => root.render(createElement(Harness)));

    await act(async () => {
      vi.advanceTimersByTime(10_000);
      await Promise.resolve();
    });

    expect(statusCallsFor('r3').length).toBe(callsBefore);
  });
});

describe('useReferenceUpload PDF dispatch', () => {
  // jsdom File sizes come from the buffer; a 50MB fixture is wasteful, so the
  // size is overridden per-instance.
  const makeFile = (name: string, size = 1024): File => {
    const f = new File([new Uint8Array(8)], name, { type: 'application/pdf' });
    Object.defineProperty(f, 'size', { value: size });
    return f;
  };

  const drop = async (file: File) => {
    await act(async () => {
      await hook!.dragHandlers.onDrop({ preventDefault: () => {}, dataTransfer: { files: [file] } } as never);
    });
  };

  const pick = async (file: File) => {
    await act(async () => {
      await hook!.handleFileInput({ target: { files: [file], value: 'x' } } as never);
    });
  };

  const armProject = () => {
    __project = { project_id: 'p1' };
    __document = { document_id: 'd1' };
    act(() => root.render(createElement(Harness)));
  };

  it('routes a dropped .pdf to /references/upload-pdf, not upload-markdown', async () => {
    armProject();
    uploadMock.mockResolvedValueOnce({ reference_id: 'ref-pdf', media_type: 'markdown', processing_status: 'queued' });
    await drop(makeFile('paper.pdf'));
    expect(uploadMock).toHaveBeenCalledTimes(1);
    expect(uploadMock.mock.calls[0][0]).toBe('/references/upload-pdf');
    expect(uploadMock.mock.calls[0][1].get('title')).toBe('paper.pdf');
  });

  it('routes a picked .pdf (file input) to /references/upload-pdf too', async () => {
    armProject();
    uploadMock.mockResolvedValueOnce({ reference_id: 'ref-pdf', media_type: 'markdown', processing_status: 'queued' });
    await pick(makeFile('Paper.PDF'));
    expect(uploadMock).toHaveBeenCalledTimes(1);
    expect(uploadMock.mock.calls[0][0]).toBe('/references/upload-pdf');
  });

  it('rejects a .pdf above the cap with the pdf toast and no upload', async () => {
    armProject();
    await drop(makeFile('big.pdf', 50 * 1024 * 1024 + 1));
    expect(uploadMock).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith('fileTooLargePdf');
  });

  it('control: an unknown text extension still routes to upload-markdown', async () => {
    armProject();
    uploadMock.mockResolvedValueOnce({ reference: { reference_id: 'ref-md' }, image_references: [] });
    await drop(makeFile('notes.xyz', 64));
    expect(uploadMock).toHaveBeenCalledTimes(1);
    expect(uploadMock.mock.calls[0][0]).toBe('/references/upload-markdown');
  });
});
