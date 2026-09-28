import { describe, it, expect } from 'vitest';
import { isRightPanelReady } from './right-panel-ready';

describe('isRightPanelReady', () => {
  it('is not ready when prefs are not loaded', () => {
    expect(
      isRightPanelReady({ projectPrefsLoaded: false, docPending: false, currentDocId: 'docA' }),
    ).toBe(false);
  });

  it('is ready when prefs loaded and doc is settled (no pending switch)', () => {
    expect(
      isRightPanelReady({ projectPrefsLoaded: true, docPending: false, currentDocId: 'docA' }),
    ).toBe(true);
  });

  it('is NOT ready on cold load / deep-link: prefs loaded but currentDocId is null while pending', () => {
    // F5 into /project/p/docB: projectPrefsLoaded flips before the doc commits,
    // currentDocId is still null. Panel must stay in loading state to avoid the
    // DEFAULT_DOC_STATE ('refs') fallback flick and wasted /references fetch.
    expect(
      isRightPanelReady({ projectPrefsLoaded: true, docPending: true, currentDocId: null }),
    ).toBe(false);
  });

  it('IS ready on in-app navigation: prefs loaded, pending, but previous doc still mounted', () => {
    // docA -> docB: urlDocId=docB, currentDocId=docA (non-null). Keep the panel
    // mounted on docA until docB commits; ChatPanel does not unmount.
    expect(
      isRightPanelReady({ projectPrefsLoaded: true, docPending: true, currentDocId: 'docA' }),
    ).toBe(true);
  });

  it('is not ready when prefs not loaded even if a doc is present and pending', () => {
    expect(
      isRightPanelReady({ projectPrefsLoaded: false, docPending: true, currentDocId: 'docA' }),
    ).toBe(false);
  });
});
