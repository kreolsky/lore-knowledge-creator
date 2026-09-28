/**
 * playback-rate — project-wide last-chosen audio speed (the audio-player
 * system's shared rate store).
 *
 * Pins the persistence contract: the store initializes from localStorage
 * (`lore.audio.playbackRate.v1`) with validation against RATES, `setRate`
 * persists the choice, invalid values are ignored, and a cross-tab `storage`
 * event (which fires only in OTHER tabs) is adopted in this tab.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

const KEY = 'lore.audio.playbackRate.v1';

// The store reads localStorage once at module init, so every test re-imports
// the module fresh (vi.resetModules + dynamic import) after seeding storage.
async function importStore() {
  return (await import('./playback-rate')).usePlaybackRateStore;
}

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
});

describe('playback-rate store', () => {
  it('initializes from a valid stored value', async () => {
    localStorage.setItem(KEY, '1.5');
    const store = await importStore();
    expect(store.getState().rate).toBe(1.5);
  });

  it('falls back to 1 for missing / corrupt / out-of-list stored values', async () => {
    localStorage.setItem(KEY, 'abc');
    expect((await importStore()).getState().rate).toBe(1);

    vi.resetModules();
    localStorage.setItem(KEY, '3');
    expect((await importStore()).getState().rate).toBe(1);

    vi.resetModules();
    localStorage.removeItem(KEY);
    expect((await importStore()).getState().rate).toBe(1);
  });

  it('setRate updates state and persists to localStorage', async () => {
    const store = await importStore();
    store.getState().setRate(2);
    expect(store.getState().rate).toBe(2);
    expect(localStorage.getItem(KEY)).toBe('2');
  });

  it('setRate ignores values outside RATES (no state change, no write)', async () => {
    const store = await importStore();
    store.getState().setRate(3);
    expect(store.getState().rate).toBe(1);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it('adopts the stored value on a cross-tab storage event', async () => {
    const store = await importStore();
    expect(store.getState().rate).toBe(1);

    // Another tab wrote the key → this tab's storage event carries it.
    localStorage.setItem(KEY, '1.5');
    window.dispatchEvent(new StorageEvent('storage', { key: KEY, newValue: '1.5' }));

    expect(store.getState().rate).toBe(1.5);
  });

  it('ignores storage events for other keys', async () => {
    const store = await importStore();
    localStorage.setItem('lore.unrelated', '1.5');
    window.dispatchEvent(new StorageEvent('storage', { key: 'lore.unrelated', newValue: '1.5' }));
    expect(store.getState().rate).toBe(1);
  });
});
