/** Tests for reference open mode — per-document tristate (+ legacy splitView read) + global splitRatio pref. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs, DEFAULT_DOC_STATE, readRefOpenMode, showsBothPanes } from './ui-store';

interface AppCtx {
  currentUser: { user_id: string } | null;
  currentProject: { project_id: string } | null;
  currentDocument: { document_id: string } | null;
}

let _ctx: AppCtx = { currentUser: null, currentProject: null, currentDocument: null };

beforeEach(() => {
  _ctx = {
    currentUser: { user_id: 'u1' },
    currentProject: { project_id: 'p1' },
    currentDocument: null,
  };
  registerAppBridge({
    getAppContext: () => _ctx,
    showToast: () => {},
  });
  clearLastSavedBlobs();
  // projectPrefsLoaded gates triggerSaveUI — emulate the normal hydrated runtime.
  useUIStore.setState({ documents: {}, projectPrefsLoaded: true });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('reference open mode — per-document tristate', () => {
  it("defaults to 'center' for an unknown document", () => {
    expect(useUIStore.getState().getRefOpenMode('docZ')).toBe('center');
  });

  it("setRefOpenMode(docA, 'panel') writes documents[docA].refOpenMode", () => {
    useUIStore.getState().setRefOpenMode('docA', 'panel');
    expect(useUIStore.getState().documents['docA'].refOpenMode).toBe('panel');
    expect(useUIStore.getState().getRefOpenMode('docA')).toBe('panel');
    expect('splitView' in useUIStore.getState().documents['docA']).toBe(false);
  });

  it('is per-document — docA split does not turn docB on', () => {
    useUIStore.getState().setRefOpenMode('docA', 'split');
    expect(useUIStore.getState().getRefOpenMode('docA')).toBe('split');
    expect(useUIStore.getState().getRefOpenMode('docB')).toBe('center');
  });

  it("compact viewport reads every stored mode as 'center' and leaves it stored", () => {
    expect(readRefOpenMode({ ...DEFAULT_DOC_STATE, refOpenMode: 'split' }, true)).toBe('center');
    expect(readRefOpenMode({ ...DEFAULT_DOC_STATE, refOpenMode: 'split' }, false)).toBe('split');
    expect(readRefOpenMode({ ...DEFAULT_DOC_STATE, splitView: true }, true)).toBe('center');
    useUIStore.getState().setRefOpenMode('docA', 'panel');
    useUIStore.setState({ compactLayout: true });
    expect(useUIStore.getState().getRefOpenMode('docA')).toBe('center');
    expect(useUIStore.getState().documents['docA'].refOpenMode).toBe('panel');
    useUIStore.setState({ compactLayout: false });
    expect(useUIStore.getState().getRefOpenMode('docA')).toBe('panel');
  });

  it("a persisted legacy {splitView: true} with no refOpenMode reads as 'split'", () => {
    useUIStore.setState({ documents: { docA: { ...DEFAULT_DOC_STATE, splitView: true } } });
    expect(useUIStore.getState().getRefOpenMode('docA')).toBe('split');
    expect(readRefOpenMode({ ...DEFAULT_DOC_STATE, splitView: false }, false)).toBe('center');
    expect(readRefOpenMode(undefined, false)).toBe('center');
  });

  it("writing 'center' over a legacy splitView:true drops the flag — it must not resurrect", () => {
    useUIStore.setState({ documents: { docA: { ...DEFAULT_DOC_STATE, splitView: true } } });
    useUIStore.getState().setRefOpenMode('docA', 'center');
    expect(useUIStore.getState().getRefOpenMode('docA')).toBe('center');
    expect('splitView' in useUIStore.getState().documents['docA']).toBe(false);
  });

  it('persists in the PUT preferences blob under documents map', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    useUIStore.getState().setRefOpenMode('docA', 'panel');

    await vi.waitFor(() => {
      const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'));
      expect(putCall).toBeDefined();
    }, { timeout: 1000 });

    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'))!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    expect(body.preferences.documents['docA'].refOpenMode).toBe('panel');
  });
});

describe('showsBothPanes — the one "document + reference visible" predicate', () => {
  it("is false for 'center' and true for every other mode", () => {
    expect(showsBothPanes('center')).toBe(false);
    expect(showsBothPanes('split')).toBe(true);
    expect(showsBothPanes('panel')).toBe(true);
  });
});

describe('pinRightPanel — pin the target doc\'s panel before a navigation', () => {
  it('writes rightPanelTab + rightPanelOpen on a doc with no entry yet', () => {
    useUIStore.getState().pinRightPanel('doc-x', 'refs');
    const d = useUIStore.getState().documents['doc-x'];
    expect(d.rightPanelTab).toBe('refs');
    expect(d.rightPanelOpen).toBe(true);
  });

  it('keeps the rest of an existing entry (refOpenMode survives the pin)', () => {
    useUIStore.getState().setRefOpenMode('doc-x', 'panel');
    useUIStore.getState().pinRightPanel('doc-x', 'chat');
    const d = useUIStore.getState().documents['doc-x'];
    expect(d.rightPanelTab).toBe('chat');
    expect(d.refOpenMode).toBe('panel');
  });
});

describe('split view — global ratio pref', () => {
  it('defaults to 0.5', () => {
    expect(useUIStore.getState().getSplitRatio()).toBe(0.5);
  });

  it('setSplitRatio stores the value and getSplitRatio reads it', () => {
    useUIStore.getState().setSplitRatio(0.65);
    expect(useUIStore.getState().getSplitRatio()).toBe(0.65);
  });

  it('clamps the ratio into [0.2, 0.8]', () => {
    useUIStore.getState().setSplitRatio(0.05);
    expect(useUIStore.getState().getSplitRatio()).toBe(0.2);
    useUIStore.getState().setSplitRatio(0.95);
    expect(useUIStore.getState().getSplitRatio()).toBe(0.8);
  });

  it('persists to the global prefs endpoint', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    useUIStore.getState().setSplitRatio(0.7);

    await vi.waitFor(() => {
      const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/_global'));
      expect(putCall).toBeDefined();
    }, { timeout: 1000 });
  });
});
