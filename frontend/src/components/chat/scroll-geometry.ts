/** Content-space geometry of the chat scroll container, shared by MessageList and PromptRail. */

// Content-space top of `anchor` relative to the scroll container's content box.
// Rect deltas normalized by scrollTop cancel user scrolling between passes and do
// not depend on which ancestor happens to be the anchor's offsetParent.
export function anchorContentTop(el: HTMLElement, anchor: HTMLElement): number {
  return anchor.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
}

// Gap left above a user message a rail jump lands on (the list's own py-3).
export const USER_JUMP_GAP = 12;

// The branch's user-message rows, top-down.
export function userRows(el: HTMLElement, userIds: Set<string>): HTMLElement[] {
  return Array.from(el.querySelectorAll<HTMLElement>('[data-message-id]'))
    .filter(node => userIds.has(node.dataset.messageId ?? ''));
}

// The scrollTop that puts `row` at the top of the viewport, below the gap.
export function userLanding(el: HTMLElement, row: HTMLElement): number {
  return Math.max(0, anchorContentTop(el, row) - USER_JUMP_GAP);
}
