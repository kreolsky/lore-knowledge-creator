/**
 * see SYSTEM: capability-tiers — the client-side voice refusal.
 *
 * startRecording (useAudioRecorder) and handleVoiceRecording (useVoiceInput)
 * no-op with an error toast when the account's can_voice flag is false — the
 * handler-side guard behind the UI hiding (a stale tab after a downgrade, a
 * hotkey, a stray editor action). The server 403 remains the guarantee; these
 * guards only make the refusal visible.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import 'fake-indexeddb/auto';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let useAudioRecorder: typeof import('./useAudioRecorder').useAudioRecorder;
let useVoiceInput: typeof import('./useVoiceInput').useVoiceInput;

let container: HTMLDivElement;
let root: Root;
let handlers: Record<string, (...args: unknown[]) => void>;
let showToast: ReturnType<typeof vi.fn>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  handlers = {};
  showToast = vi.fn();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);

  const appState = {
    currentProject: { project_id: 'p1', voice_recording_doc_id: null },
    currentDocument: { document_id: 'd1', title: 'Doc' },
    accessLevel: 'full',
    documents: [{ document_id: 'd1', title: 'Doc' }],
    currentUser: { user_id: 'u1', can_voice: false },
    setRecording: vi.fn(),
    showToast,
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('./useEvent', () => ({
    useEvent: (name: string, cb: (...args: unknown[]) => void) => { handlers[name] = cb; },
  }));
  vi.doMock('../editor/active-editor', () => ({ getEditorView: () => null }));

  ({ useAudioRecorder } = await import('./useAudioRecorder'));
  ({ useVoiceInput } = await import('./useVoiceInput'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => { try { root.unmount(); } catch { /* already unmounted */ } });
  container.remove();
  vi.unstubAllGlobals();
  vi.doUnmock('../store/app-store');
  vi.doUnmock('./useEvent');
  vi.doUnmock('../editor/active-editor');
});

function RecorderHarness() {
  useAudioRecorder(() => {});
  const { handleVoiceRecording } = useVoiceInput();
  void handleVoiceRecording; // the voice-input half is driven directly below
  return null;
}

describe('capability tiers — client voice refusal', () => {
  it('startRecording no-ops with the tier toast; no recorder, no cache session', async () => {
    await act(async () => { root.render(createElement(RecorderHarness)); });
    await act(async () => { await (handlers['start-recording'] as () => Promise<void>)(); });

    const { t } = await import('../i18n');
    expect(showToast).toHaveBeenCalledWith(t('tierVoiceRequired'), 'error');
    // Not a silent partial start: no recording state was set.
    expect((await import('../store/app-store')).useAppStore.getState().setRecording)
      .not.toHaveBeenCalled();
  });

  it('handleVoiceRecording refuses the sync upload with the tier toast', async () => {
    let handle: ((file: File, ctx: { projectId: string; documentId: string; sessionId: string }) => Promise<boolean>) | undefined;
    function VoiceHarness() {
      const { handleVoiceRecording } = useVoiceInput();
      handle = handleVoiceRecording;
      return null;
    }
    await act(async () => { root.render(createElement(VoiceHarness)); });
    if (!handle) throw new Error('voice harness did not bind handleVoiceRecording');
    const invokeVoice = handle;
    const file = new File([new Blob(['audio'])], 'voice.webm', { type: 'audio/webm' });
    let handled: boolean | undefined;
    await act(async () => { handled = await invokeVoice(file, { projectId: 'p1', documentId: 'd1', sessionId: 's1' }); });

    const { t } = await import('../i18n');
    expect(handled).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(t('tierVoiceRequired'), 'error');
  });
});
