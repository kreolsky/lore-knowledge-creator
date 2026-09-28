/** Tests for the blocking-overlay grace controller. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createOverlayGrace } from '../overlay-grace';

describe('createOverlayGrace', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('initial connecting reveals the overlay immediately (no prior good state)', () => {
    const changes: boolean[] = [];
    const grace = createOverlayGrace((h) => changes.push(h), 1500);
    grace.update(false, false); // connecting, never been ready
    expect(changes).toEqual([false]); // hidden=false → overlay shown
  });

  it('connected → reconnecting → connected within grace never shows the overlay', () => {
    const changes: boolean[] = [];
    const grace = createOverlayGrace((h) => changes.push(h), 1500);
    grace.update(true, false);       // connected (hidden stays true → no change emitted)
    grace.update(false, false);      // mid-session drop — starts grace timer
    vi.advanceTimersByTime(1000);    // under grace
    grace.update(true, false);       // recovered before timer fires
    vi.advanceTimersByTime(2000);    // timer (if any) would have fired by now

    // Overlay must never have been revealed.
    expect(changes).not.toContain(false);
  });

  it('a drop longer than grace reveals the overlay', () => {
    const changes: boolean[] = [];
    const grace = createOverlayGrace((h) => changes.push(h), 1500);
    grace.update(true, false);
    grace.update(false, false);
    vi.advanceTimersByTime(1600);
    expect(changes).toContain(false);
  });

  it('offline reveals immediately, skipping grace', () => {
    const changes: boolean[] = [];
    const grace = createOverlayGrace((h) => changes.push(h), 1500);
    grace.update(true, false);
    grace.update(false, true); // offline
    expect(changes).toEqual([false]);
  });
});
