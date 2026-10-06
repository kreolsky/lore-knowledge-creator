/** useHoverPreview: a touch tap's compat mouseenter must not open a preview; a mouse hover must. */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useHoverPreview } from './useHoverPreview';

// jsdom has no PointerEvent constructor — a plain Event carrying pointerType is what the tracker reads.
function pointerOver(pointerType: string) {
  const e = new Event('pointerover', { bubbles: true });
  Object.defineProperty(e, 'pointerType', { value: pointerType });
  document.body.dispatchEvent(e);
}

function hoverOnce(): boolean {
  const { result } = renderHook(() => useHoverPreview({ getPopupLeft: () => 0 }));
  act(() => { result.current.handleHover(document.body); });
  act(() => { vi.advanceTimersByTime(1000); });
  return result.current.visible;
}

describe('useHoverPreview pointer gate', () => {
  afterEach(() => { pointerOver('mouse'); vi.useRealTimers(); });

  it('does not open after touch input', () => {
    vi.useFakeTimers();
    pointerOver('touch');
    expect(hoverOnce()).toBe(false);
  });

  it('opens after mouse input', () => {
    vi.useFakeTimers();
    pointerOver('mouse');
    expect(hoverOnce()).toBe(true);
  });
});
