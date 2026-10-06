/** Last pointer type the user interacted with — gates hover-only UI on touch input. */

// WHY: a touch tap fires compatibility mouseenter/mouseover, so hover-preview listeners
// open a popup on top of whatever the tap just navigated to. `pointerover` precedes those
// compat events and carries pointerType; tracking it per interaction (not a media query)
// keeps previews on hybrid devices whenever the actual input is a mouse/trackpad.
let lastPointerType = 'mouse';

if (typeof document !== 'undefined') {
  document.addEventListener('pointerover', e => { lastPointerType = e.pointerType; }, { capture: true, passive: true });
}

export function isTouchPointer(): boolean {
  return lastPointerType === 'touch';
}
