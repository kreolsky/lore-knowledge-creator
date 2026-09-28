/** Tests for the single-active floating-popup slot store. */

import { describe, it, expect, beforeEach } from 'vitest';
import { usePopupStore } from './popup-store';

function reset() {
  usePopupStore.setState({ activeId: null, activePriority: 0, activeToken: 0, _nextToken: 1 });
}

beforeEach(reset);

describe('popup-store', () => {
  it('grants the slot into an empty store and returns a token', () => {
    const token = usePopupStore.getState().requestOpen('hover-preview');
    expect(token).not.toBeNull();
    expect(usePopupStore.getState().activeId).toBe('hover-preview');
    expect(usePopupStore.getState().activeToken).toBe(token);
  });

  it('lets a higher-priority popup take over a lower one', () => {
    usePopupStore.getState().requestOpen('hover-preview');           // prio 10
    const token = usePopupStore.getState().requestOpen('parent-picker'); // prio 30
    expect(token).not.toBeNull();
    expect(usePopupStore.getState().activeId).toBe('parent-picker');
  });

  it('lets an equal-priority popup take over (newest wins)', () => {
    usePopupStore.getState().requestOpen('content-picker');          // prio 20
    const token = usePopupStore.getState().requestOpen('link-suggestions'); // prio 20
    expect(token).not.toBeNull();
    expect(usePopupStore.getState().activeId).toBe('link-suggestions');
  });

  it('suppresses a lower-priority popup while a higher one is active', () => {
    usePopupStore.getState().requestOpen('parent-picker');           // prio 30
    const token = usePopupStore.getState().requestOpen('hover-preview'); // prio 10
    expect(token).toBeNull();
    expect(usePopupStore.getState().activeId).toBe('parent-picker'); // unchanged
  });

  it('releasing the active token frees the slot', () => {
    const token = usePopupStore.getState().requestOpen('parent-picker')!;
    usePopupStore.getState().release(token);
    expect(usePopupStore.getState().activeId).toBeNull();
    expect(usePopupStore.getState().activeToken).toBe(0);
  });

  it('releasing a stale token is a no-op and does not clobber the active owner', () => {
    const stale = usePopupStore.getState().requestOpen('hover-preview')!;
    const fresh = usePopupStore.getState().requestOpen('parent-picker')!; // takes over; stale token now obsolete
    usePopupStore.getState().release(stale);                              // must NOT free the slot
    expect(usePopupStore.getState().activeId).toBe('parent-picker');
    expect(usePopupStore.getState().activeToken).toBe(fresh);
  });

  it('does not auto-restore a suppressed lower popup when the higher one closes', () => {
    const high = usePopupStore.getState().requestOpen('parent-picker')!;
    usePopupStore.getState().requestOpen('hover-preview'); // suppressed (null)
    usePopupStore.getState().release(high);
    expect(usePopupStore.getState().activeId).toBeNull(); // nothing restored
  });

  it('issues monotonically increasing tokens', () => {
    const t1 = usePopupStore.getState().requestOpen('hover-preview')!;
    usePopupStore.getState().release(t1);
    const t2 = usePopupStore.getState().requestOpen('hover-preview')!;
    expect(t2).toBeGreaterThan(t1);
  });
});
