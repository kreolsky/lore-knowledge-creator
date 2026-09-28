/**
 * SYSTEM: audio-player — project-wide playback-speed choice (shared rate store).
 *
 * ARCH: the last user-chosen speed is shared by ALL AudioPlayer instances and
 * survives remount / reference switch / page reload. Kept in localStorage
 * under `lore.audio.playbackRate.v1`, NOT in the /api/preferences/_global
 * blob: public shares /s/:token are anonymous and MUST NOT call the
 * auth-gated preferences API (see the INVARIANT in ui-store.ts
 * triggerSaveGlobalPrefs). Precedent: last-used highlight color
 * (markdown-actions.ts LAST_COLOR_KEY). Cross-device sync is out of scope.
 * A failed read (quota / private mode / corrupt value) falls back to 1; a
 * failed write keeps the in-memory rate — a non-critical preference must not
 * surface toasts.
 */
import { create } from 'zustand';

export const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];

const KEY = 'lore.audio.playbackRate.v1';

// null / corrupt / out-of-list / throwing localStorage → 1.
function readStoredRate(): number {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === null) return 1;
    const n = Number(raw);
    return RATES.includes(n) ? n : 1;
  } catch {
    return 1;
  }
}

export interface PlaybackRateState {
  rate: number;
  setRate: (r: number) => void;
}

export const usePlaybackRateStore = create<PlaybackRateState>(set => ({
  rate: readStoredRate(),
  setRate: (r: number) => {
    if (!RATES.includes(r)) return;
    set({ rate: r });
    try {
      localStorage.setItem(KEY, String(r));
    } catch {
      // Quota / private mode — the in-memory rate still applies everywhere.
    }
  },
}));

// Cross-tab sync: the `storage` event fires only in OTHER tabs/windows — in
// THIS tab setRate already updated the state. Re-read + validate rather than
// trusting e.newValue.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', e => {
    if (e.key !== KEY) return;
    usePlaybackRateStore.setState({ rate: readStoredRate() });
  });
}

export const usePlaybackRate = () => usePlaybackRateStore(s => s.rate);
