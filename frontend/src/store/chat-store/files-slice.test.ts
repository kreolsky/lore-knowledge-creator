// @vitest-environment jsdom
/** files-slice — a dropped file is a chip while it uploads into the sandbox
 * workspace, `done` with the server's record after, and goes back out of the
 * workspace when its chip is removed before the send. */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

const showToast = vi.fn();
vi.mock('../app-store', () => ({
  useAppStore: { getState: () => ({ showToast }) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));

type Deferred = { resolve: (v: unknown) => void; reject: (e: unknown) => void; abort: ReturnType<typeof vi.fn> };
const uploads: Deferred[] = [];
const progressCbs: Array<(f: number) => void> = [];
vi.mock('../../api/sandbox-attachments', () => {
  class AttachmentUploadError extends Error {
    status: number;
    constructor(status: number) { super(String(status)); this.status = status; }
  }
  return {
    AttachmentUploadError,
    uploadChatAttachment: vi.fn((_pid: string, _file: File, onProgress: (f: number) => void) => {
      progressCbs.push(onProgress);
      const d = { abort: vi.fn() } as unknown as Deferred;
      const promise = new Promise((resolve, reject) => { d.resolve = resolve; d.reject = reject; });
      uploads.push(d);
      return { promise, abort: d.abort };
    }),
    deleteChatAttachment: vi.fn(() => Promise.resolve()),
  };
});

import { createFilesSlice } from './files-slice';
import { AttachmentUploadError, deleteChatAttachment } from '../../api/sandbox-attachments';
import type { ChatState } from './types';

const UPLOADED = { id: 'f1', name: 'a.pdf', size: 4, mime: 'application/pdf', path: '/workspace/x/inbox/chat/f1/a.pdf' };

function store() {
  return create<ChatState>((set, get) => ({
    pendingFiles: [],
    ...createFilesSlice(set, get),
  } as unknown as ChatState));
}

const pdf = () => new File(['%PDF'], 'a.pdf', { type: 'application/pdf' });
const flush = () => new Promise(r => setTimeout(r, 0));

beforeEach(() => {
  vi.clearAllMocks();
  uploads.length = 0;
  progressCbs.length = 0;
});

describe('files-slice', () => {
  it('a drop shows an uploading chip, then a done chip carrying the server record', async () => {
    const st = store();
    st.getState().addPendingFile('p1', pdf());
    expect(st.getState().pendingFiles).toMatchObject([{ name: 'a.pdf', status: 'uploading', progress: 0 }]);
    progressCbs[0](0.5);
    expect(st.getState().pendingFiles[0].progress).toBe(0.5);
    uploads[0].resolve(UPLOADED);
    await flush();
    expect(st.getState().pendingFiles).toMatchObject([{ status: 'done', uploaded: UPLOADED }]);
  });

  it('a refused upload turns the chip into an error and says why', async () => {
    const st = store();
    st.getState().addPendingFile('p1', pdf());
    uploads[0].reject(new AttachmentUploadError(503));
    await flush();
    expect(st.getState().pendingFiles[0].status).toBe('error');
    expect(showToast).toHaveBeenCalledWith('chatFileNoSandbox', 'error');
  });

  it('removing a done chip takes the file back out of the workspace', async () => {
    const st = store();
    st.getState().addPendingFile('p1', pdf());
    uploads[0].resolve(UPLOADED);
    await flush();
    st.getState().removePendingFile(st.getState().pendingFiles[0].key);
    expect(st.getState().pendingFiles).toEqual([]);
    expect(deleteChatAttachment).toHaveBeenCalledWith('p1', 'f1');
  });

  it('removing an uploading chip aborts it, and a file that landed anyway goes back out', async () => {
    const st = store();
    st.getState().addPendingFile('p1', pdf());
    st.getState().removePendingFile(st.getState().pendingFiles[0].key);
    expect(uploads[0].abort).toHaveBeenCalled();
    uploads[0].resolve(UPLOADED);
    await flush();
    expect(st.getState().pendingFiles).toEqual([]);
    expect(deleteChatAttachment).toHaveBeenCalledWith('p1', 'f1');
  });

  it('a failed take-back is said, not swallowed', async () => {
    vi.mocked(deleteChatAttachment).mockRejectedValueOnce(new Error('down'));
    const st = store();
    st.getState().restorePendingFiles('p1', [UPLOADED]);
    st.getState().removePendingFile(st.getState().pendingFiles[0].key);
    await flush();
    expect(showToast).toHaveBeenCalledWith('chatFileRemoveFailed', 'error');
  });

  it('clearing after a send leaves the files in the workspace', async () => {
    const st = store();
    st.getState().restorePendingFiles('p1', [UPLOADED]);
    st.getState().clearPendingFiles();
    expect(st.getState().pendingFiles).toEqual([]);
    expect(deleteChatAttachment).not.toHaveBeenCalled();
  });
});
