/** useSimpleVoiceRecording — cancelRecording discards the take; a plain stop still transcribes. */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const showToast = vi.fn();
vi.mock('../store/app-store', () => ({ useAppStore: { getState: () => ({ showToast }) } }));
vi.mock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { useSimpleVoiceRecording } from './useSimpleVoiceRecording';

class FakeRecorder {
  static last: FakeRecorder;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  state = 'inactive';
  constructor(public stream: MediaStream) { FakeRecorder.last = this; }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: new Blob(['audio']) });
    this.onstop?.();
  }
}

type Api = ReturnType<typeof useSimpleVoiceRecording>;
let api: Api;
let root: Root;
let host: HTMLDivElement;
const onTranscribed = vi.fn();
const trackStop = vi.fn();
const fetchMock = vi.fn();

function Probe() { api = useSimpleVoiceRecording(onTranscribed); return null; }

beforeEach(() => {
  vi.clearAllMocks();
  const stream = { getTracks: () => [{ stop: trackStop }] } as unknown as MediaStream;
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(async () => stream) } });
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  fetchMock.mockResolvedValue({ ok: true, json: async () => ({ text: 'hello' }) });
  vi.stubGlobal('fetch', fetchMock);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => { root.render(createElement(Probe)); });
});

afterEach(() => {
  act(() => { root.unmount(); });
  host.remove();
  vi.unstubAllGlobals();
});

describe('useSimpleVoiceRecording', () => {
  it('cancelRecording discards the take: no transcription request, no toast, mic released', async () => {
    await act(async () => { await api.toggleRecording(); });
    expect(api.recording).toBe(true);
    await act(async () => { api.cancelRecording(); });
    expect(api.recording).toBe(false);
    expect(trackStop).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
    expect(onTranscribed).not.toHaveBeenCalled();
  });

  it('a plain stop still transcribes, also after an earlier cancel', async () => {
    await act(async () => { await api.toggleRecording(); });
    await act(async () => { api.cancelRecording(); });
    await act(async () => { await api.toggleRecording(); });
    await act(async () => { await api.toggleRecording(); });
    expect(fetchMock).toHaveBeenCalledWith('/api/chat/transcribe', expect.objectContaining({ method: 'POST' }));
    expect(onTranscribed).toHaveBeenCalledWith('hello');
  });
});
