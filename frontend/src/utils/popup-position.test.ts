/** Tests for popup positioning helpers, incl. the right-panel side-of-panel
 *  placement used by EditorLinkPreview for chat links (.chat-source/.chat-link)
 *  inside <aside class="right-panel">. */

import { describe, it, expect, afterEach } from 'vitest';
import {
  computePopupPosition,
  clampPopupLeft,
  getRightPanelPopupLeft,
  chooseLinkPlacement,
  computeBesideBoxPreviewPosition,
  RIGHT_PANEL_PREVIEW_SHIFT,
} from './popup-position';
import { PREVIEW_WIDTH, PREVIEW_GAP, PREVIEW_PICKER_MAX_HEIGHT } from './preview-geometry';

function rect(overrides: Partial<DOMRect> = {}): DOMRect {
  return {
    top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0, x: 0, y: 0,
    toJSON: () => ({}),
    ...overrides,
  } as DOMRect;
}

function makeEl(rectValue: DOMRect): HTMLElement {
  const el = document.createElement('span');
  el.getBoundingClientRect = () => rectValue;
  return el;
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('RIGHT_PANEL_PREVIEW_SHIFT', () => {
  it('is 60 (parity with useRightPanelHoverPreview)', () => {
    expect(RIGHT_PANEL_PREVIEW_SHIFT).toBe(60);
  });
});

describe('getRightPanelPopupLeft', () => {
  it('pins the popup to the left of .right-panel (panelLeft - W - gap)', () => {
    const panel = document.createElement('aside');
    panel.className = 'right-panel';
    panel.getBoundingClientRect = () => rect({ left: 900, width: 360 });
    document.body.appendChild(panel);

    expect(getRightPanelPopupLeft(rect({ left: 920 }))).toBe(900 - PREVIEW_WIDTH - PREVIEW_GAP);
  });

  it('floors at 4 when the panel sits near the left edge', () => {
    const panel = document.createElement('aside');
    panel.className = 'right-panel';
    panel.getBoundingClientRect = () => rect({ left: 40 });
    document.body.appendChild(panel);

    expect(getRightPanelPopupLeft(rect({ left: 60 }))).toBe(4);
  });

  it('falls back to the anchor rect.left (no throw) when no panel is in the DOM', () => {
    expect(getRightPanelPopupLeft(rect({ left: 50 }))).toBe(4);
    expect(getRightPanelPopupLeft(rect({ left: 920 }))).toBe(920 - PREVIEW_WIDTH - PREVIEW_GAP);
  });
});

describe('chooseLinkPlacement', () => {
  it('uses side-of-panel placement + vertical shift for a link inside .right-panel', () => {
    const panel = document.createElement('aside');
    panel.className = 'right-panel';
    panel.getBoundingClientRect = () => rect({ left: 900, width: 360 });
    document.body.appendChild(panel);

    const link = makeEl(rect({ left: 920, top: 200, bottom: 216 }));
    panel.appendChild(link);

    const placed = chooseLinkPlacement(link, link.getBoundingClientRect());
    expect(placed.left).toBe(900 - PREVIEW_WIDTH - PREVIEW_GAP);
    expect(placed.topShift).toBe(60);
  });

  it('keeps near-link placement + zero shift for an editor link (no panel ancestor)', () => {
    const linkRect = rect({ left: 920, top: 200, bottom: 216 });
    const link = makeEl(linkRect);
    document.body.appendChild(link);

    const placed = chooseLinkPlacement(link, linkRect);
    expect(placed.left).toBe(clampPopupLeft(linkRect.left));
    expect(placed.topShift).toBe(0);
  });
});

describe('existing helpers (regression)', () => {
  it('clampPopupLeft clamps into the viewport', () => {
    expect(clampPopupLeft(4)).toBe(4);
  });

  it('computePopupPosition returns a maxH >= MIN_HEIGHT', () => {
    const pos = computePopupPosition(rect({ top: 100, bottom: 120 }), 340);
    expect(pos.maxH).toBeGreaterThanOrEqual(40);
  });
});

describe('computeBesideBoxPreviewPosition', () => {
  it('places the preview to the right of the box when there is room', () => {
    // box [100..440], viewport 1024 → 584px to the right, fits PREVIEW_WIDTH+GAP
    const pos = computeBesideBoxPreviewPosition(100, 50, 340, PREVIEW_PICKER_MAX_HEIGHT);
    expect(pos.top).toBe(50);
    expect(pos.left).toBe(440 + PREVIEW_GAP);
    expect(pos.maxHeight).toBe(PREVIEW_PICKER_MAX_HEIGHT);
  });

  it('flips to the left of the box when the right side has no room', () => {
    // box [700..1040] overflows the 1024 viewport on the right → flip left
    const pos = computeBesideBoxPreviewPosition(700, 50, 340, PREVIEW_PICKER_MAX_HEIGHT);
    expect(pos.left).toBe(700 - PREVIEW_WIDTH - PREVIEW_GAP);
  });

  it('clamps maxHeight to the viewport minus the default bottom margin', () => {
    // boxTop 600, viewport height 768, default margin 20 → 148px of room (< cap)
    const pos = computeBesideBoxPreviewPosition(100, 600, 340, PREVIEW_PICKER_MAX_HEIGHT);
    expect(pos.maxHeight).toBe(768 - 600 - 20);
  });

  it('honours a custom bottom margin', () => {
    const pos = computeBesideBoxPreviewPosition(100, 600, 340, PREVIEW_PICKER_MAX_HEIGHT, 12);
    expect(pos.maxHeight).toBe(768 - 600 - 12);
  });
});
