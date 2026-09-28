/**
 * AudioPlayer — custom audio controls (replaces native <audio controls>).
 *
 * Pins the contract: a hidden <audio> drives decoding, but the controls are our
 * own (play/pause, seek, time, volume/mute, speed Dropdown opening DOWNWARD,
 * download). jsdom has no media playback, so HTMLMediaElement getters/setters
 * and play() are stubbed to backing fields; React events are fired manually.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let AudioPlayer: typeof import('./AudioPlayer').AudioPlayer;
let showToast: ReturnType<typeof vi.fn>;

// jsdom's HTMLMediaElement exposes read-only-ish getters (duration=NaN) and a
// play() that rejects. Replace the fields we touch with plain get/set backing
// stores so the component's event handlers read/write real numbers.
function stubMedia() {
  const proto = HTMLMediaElement.prototype as unknown as Record<string, PropertyDescriptor>;
  const fields: Record<string, number | boolean> = {
    duration: NaN, currentTime: 0, playbackRate: 1, volume: 1, muted: false,
  };
  for (const [name, def] of Object.entries(fields)) {
    let store = def;
    Object.defineProperty(HTMLMediaElement.prototype, name, {
      configurable: true,
      get: () => store,
      set: (v: number | boolean) => { store = v; },
    });
    void proto;
  }
  HTMLMediaElement.prototype.play = () => Promise.resolve() as unknown as Promise<void>;
  HTMLMediaElement.prototype.pause = () => {};
}

beforeEach(async () => {
  vi.resetModules();
  stubMedia();
  // The playback-rate store reads localStorage at import; without clearing,
  // a rate written by one test leaks into the next test's default-1× setup.
  localStorage.clear();

  showToast = vi.fn();
  // app-store import pulls in CodeMirror/collab — mock the one method we use.
  const useAppStore: any = () => {};
  useAppStore.getState = () => ({ showToast });
  useAppStore.subscribe = () => () => {};
  vi.doMock('../../store/app-store', () => ({ useAppStore }));

  // useTranslation reads language from ui-store.
  const useUIStore: any = (selector?: (s: any) => any) =>
    selector ? selector({ language: 'en' }) : { language: 'en' };
  useUIStore.getState = () => ({ language: 'en' });
  useUIStore.subscribe = () => () => {};
  vi.doMock('../../store/ui-store', () => ({ useUIStore }));

  AudioPlayer = (await import('./AudioPlayer')).AudioPlayer;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../../store/app-store');
  vi.doUnmock('../../store/ui-store');
});

function renderPlayer(props: Partial<import('./AudioPlayer').AudioPlayerProps> = {}) {
  act(() => root.render(createElement(AudioPlayer, {
    src: '/api/files/r1/audio/interview.mp3',
    title: 'Interview',
    ...props,
  })));
}

const query = (sel: string) => container.querySelector(sel)!;
const queryAll = (sel: string) => Array.from(container.querySelectorAll(sel));
const click = (el: Element) => (el as HTMLElement).click();

describe('AudioPlayer', () => {
  it('renders play/pause button + speed Dropdown with all six rate options', () => {
    renderPlayer();

    // Play button present (aria via i18n key `play`).
    expect(query('button[aria-label="Play"]')).toBeTruthy();

    // Open the speed menu and assert six rate options.
    act(() => { click(query('button[title="Speed"]')); });
    const options = queryAll('[role="option"]');
    expect(options.map(o => o.textContent)).toEqual([
      '0.5×', '0.75×', '1×', '1.25×', '1.5×', '2×',
    ]);
  });

  it('selecting 1.5× sets audio.playbackRate and the trigger label', () => {
    renderPlayer();
    const audio = query('audio') as HTMLAudioElement;

    act(() => { click(query('button[title="Speed"]')); });
    const rate15 = queryAll('[role="option"]').find(o => o.textContent === '1.5×')!;
    act(() => { click(rate15); });

    expect(audio.playbackRate).toBe(1.5);
    // Closed menu → trigger now shows the selected rate.
    expect(query('button[title="Speed"]').textContent).toContain('1.5×');
  });

  it('changing the seek slider sets audio.currentTime', () => {
    renderPlayer();
    const audio = query('audio') as HTMLAudioElement;

    // Simulate metadata load so duration becomes finite and the seek enables.
    (audio as unknown as { duration: number }).duration = 60;
    act(() => audio.dispatchEvent(new Event('loadedmetadata', { bubbles: true })));

    const seek = query('input.audio-player-range') as HTMLInputElement;
    // Native setter so React's value-tracker registers the change (direct .value
    // assignment is silently ignored by controlled-input change detection).
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    act(() => {
      setValue.call(seek, 12.3);
      seek.dispatchEvent(new Event('input', { bubbles: true }));
      seek.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(audio.currentTime).toBeCloseTo(12.3, 1);
  });

  it('surfaces an explicit error state on media error (no silent dead UI) + toast', () => {
    renderPlayer();
    const audio = query('audio') as HTMLAudioElement;

    act(() => {
      audio.dispatchEvent(new Event('error', { bubbles: true }));
    });

    // In-bar error text present (distinct from a silent disabled control).
    expect(container.textContent).toContain('Failed to load audio');
    expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error');
  });

  it('enables the seek bar for an Infinity-duration stream once it buffers (WebM/Opus)', () => {
    // WebM/Opus over HTTP reports duration === Infinity until fully buffered;
    // the seek bar must not stay permanently disabled — it should follow the
    // buffered (seekable) range instead.
    renderPlayer();
    const audio = query('audio') as HTMLAudioElement;

    // Before any buffer: disabled, ceiling 0.
    const seek = () => query('input.audio-player-range') as HTMLInputElement;
    expect(seek().disabled).toBe(true);
    expect(seek().max).toBe('0');

    // Emulate the stream: Infinity duration, 30s buffered.
    (audio as unknown as { duration: number }).duration = Infinity;
    Object.defineProperty(audio, 'seekable', {
      configurable: true,
      get: () => ({ length: 1, start: () => 0, end: () => 30 }),
    });
    act(() => audio.dispatchEvent(new Event('progress', { bubbles: true })));

    expect(seek().disabled).toBe(false);
    expect(Number(seek().max)).toBe(30);
  });

  it('clears a stale error + seek state when src changes (reference switch reuses the instance)', () => {
    // ReferenceMediaBar is rendered without a key, so switching references reuses
    // this AudioPlayer — a prior load error must not persist over the next ref.
    renderPlayer({ src: '/api/files/r1/a.webm' });
    const audio = query('audio') as HTMLAudioElement;
    act(() => audio.dispatchEvent(new Event('error', { bubbles: true })));
    expect(container.textContent).toContain('Failed to load audio');

    // New reference, same instance.
    act(() => root.render(createElement(AudioPlayer, { src: '/api/files/r2/b.mp3', title: 'B' })));

    expect(container.textContent).not.toContain('Failed to load audio');
    // Seek bar renders again (error state cleared).
    expect(query('input.audio-player-range')).toBeTruthy();
  });

  // ─── durationSec prop: known total drives the bar before the element knows ──

  // The total-time label is the tabular span AFTER the range input.
  const totalTimeLabel = () => {
    const spans = queryAll('span.tabular-nums');
    return spans[spans.length - 1].textContent;
  };

  it('uses durationSec for slider max + total-time before any metadata event', () => {
    // A duration-less WebM reports Infinity until buffered; durationSec is the
    // known total, so the bar + label must be correct on first paint.
    renderPlayer({ durationSec: 120 });
    const seek = query('input.audio-player-range') as HTMLInputElement;
    expect(seek.disabled).toBe(false);        // ceiling known → enabled immediately
    expect(Number(seek.max)).toBe(120);
    expect(totalTimeLabel()).toBe('2:00');
  });

  it('a finite audio.duration overrides the durationSec prop', () => {
    // The element is the source of truth once it knows — a wrong stored number
    // can never outlive the real one.
    renderPlayer({ durationSec: 120 });
    const audio = query('audio') as HTMLAudioElement;
    (audio as unknown as { duration: number }).duration = 60;
    act(() => audio.dispatchEvent(new Event('loadedmetadata', { bubbles: true })));

    const seek = query('input.audio-player-range') as HTMLInputElement;
    expect(Number(seek.max)).toBe(60);
    expect(totalTimeLabel()).toBe('1:00');
  });

  it('src-change reset clears to the new durationSec prop, not to 0', () => {
    // Switching references reuses the instance; the reset effect must adopt the
    // NEW prop's ceiling, not zero out the bar until metadata arrives.
    renderPlayer({ src: '/api/files/r1/a.webm', durationSec: 120 });
    act(() => root.render(createElement(AudioPlayer, {
      src: '/api/files/r2/b.webm', title: 'B', durationSec: 90,
    })));

    const seek = query('input.audio-player-range') as HTMLInputElement;
    expect(Number(seek.max)).toBe(90);
    expect(totalTimeLabel()).toBe('1:30');
  });

  // ─── playback rate: shared last choice, persisted in localStorage ──

  it('selecting 1.5× persists the choice to localStorage', () => {
    renderPlayer();

    act(() => { click(query('button[title="Speed"]')); });
    const rate15 = queryAll('[role="option"]').find(o => o.textContent === '1.5×')!;
    act(() => { click(rate15); });

    expect(localStorage.getItem('lore.audio.playbackRate.v1')).toBe('1.5');
  });

  it('adopts the persisted rate on mount (pre-seeded localStorage)', async () => {
    localStorage.setItem('lore.audio.playbackRate.v1', '2');
    // The store reads localStorage at module init — re-import so the fresh
    // playback-rate store initializes from the seeded value (on a real page
    // load storage is populated before the app's modules evaluate).
    vi.resetModules();
    AudioPlayer = (await import('./AudioPlayer')).AudioPlayer;
    renderPlayer();

    const audio = query('audio') as HTMLAudioElement;
    expect(audio.playbackRate).toBe(2);
    expect(query('button[title="Speed"]').textContent).toContain('2×');
  });

  it('falls back to 1× when the stored rate is out-of-list', async () => {
    localStorage.setItem('lore.audio.playbackRate.v1', '7');
    vi.resetModules();
    AudioPlayer = (await import('./AudioPlayer')).AudioPlayer;
    renderPlayer();

    const audio = query('audio') as HTMLAudioElement;
    expect(audio.playbackRate).toBe(1);
    expect(query('button[title="Speed"]').textContent).toContain('1×');
  });

  it('keeps the chosen rate across a src switch (instance reuse)', () => {
    renderPlayer({ src: '/api/files/r1/a.webm' });
    const audio = query('audio') as HTMLAudioElement;

    act(() => { click(query('button[title="Speed"]')); });
    const rate15 = queryAll('[role="option"]').find(o => o.textContent === '1.5×')!;
    act(() => { click(rate15); });
    expect(audio.playbackRate).toBe(1.5);

    // New reference, same instance — the rate must survive the switch.
    act(() => root.render(createElement(AudioPlayer, { src: '/api/files/r2/b.mp3', title: 'B' })));

    expect(audio.playbackRate).toBe(1.5);
    expect(query('button[title="Speed"]').textContent).toContain('1.5×');
  });

  it('shares the chosen rate across players (project-wide last choice)', () => {
    // Two players in one page: changing the rate in one must reach the other
    // player's media element and trigger label.
    renderPlayer();
    const container2 = document.createElement('div');
    document.body.appendChild(container2);
    const root2 = createRoot(container2);
    try {
      act(() => root2.render(createElement(AudioPlayer, {
        src: '/api/files/r9/z.mp3', title: 'Z',
      })));
      const audio1 = query('audio') as HTMLAudioElement;
      const audio2 = container2.querySelector('audio') as HTMLAudioElement;

      act(() => { click(query('button[title="Speed"]')); });
      const rate15 = queryAll('[role="option"]').find(o => o.textContent === '1.5×')!;
      act(() => { click(rate15); });

      expect(audio1.playbackRate).toBe(1.5);
      expect(audio2.playbackRate).toBe(1.5);
      expect(container2.querySelector('button[title="Speed"]')!.textContent).toContain('1.5×');
    } finally {
      act(() => root2.unmount());
      container2.remove();
    }
  });
});
