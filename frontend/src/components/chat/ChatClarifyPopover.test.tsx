/** Tests for ChatClarifyPopover: the button follows the selection the browser
 * leaves AFTER mouseup, not the one still present while mouseup dispatches. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../hooks/useSimpleVoiceRecording', () => ({
  useSimpleVoiceRecording: () => ({ recording: false, transcribing: false, toggleRecording: () => {} }),
}));

import { ChatClarifyPopover } from './ChatClarifyPopover';

let container: HTMLDivElement;
let root: Root;
let textNode: Text;

function selectAll() {
  const range = document.createRange();
  range.selectNodeContents(textNode);
  const sel = window.getSelection()!;
  sel.removeAllRanges();
  sel.addRange(range);
}

// Mimics the browser: a click inside an existing selection collapses it only as
// mouseup's default action, i.e. after every mouseup listener has already run.
function mouseUp(collapseAfterDispatch: boolean) {
  act(() => {
    textNode.parentElement!.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 10, clientY: 10 }));
    if (collapseAfterDispatch) window.getSelection()!.collapse(textNode, 2);
  });
  act(() => { vi.runAllTimers(); });
}

function clarifyButton(): Element | null {
  return document.body.querySelector('.fixed button');
}

beforeEach(() => {
  vi.useFakeTimers();
  Range.prototype.getBoundingClientRect = () => ({ width: 50, height: 10 }) as DOMRect;
  container = document.createElement('div');
  container.innerHTML = '<div class="chat-markdown"><p>assistant answer text</p></div>';
  document.body.appendChild(container);
  textNode = container.querySelector('p')!.firstChild as Text;
  root = createRoot(document.createElement('div'));
  act(() => { root.render(createElement(ChatClarifyPopover)); });
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
  window.getSelection()!.removeAllRanges();
  vi.useRealTimers();
});

describe('ChatClarifyPopover', () => {
  it('shows the button for a selection inside a chat bubble', () => {
    selectAll();
    mouseUp(false);
    expect(clarifyButton()).not.toBeNull();
  });

  it('hides the button when a click inside the selection collapses it', () => {
    selectAll();
    mouseUp(false);
    expect(clarifyButton()).not.toBeNull();
    // Click inside the selected block: mousedown keeps the selection, mouseup clears it.
    act(() => { textNode.parentElement!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); });
    mouseUp(true);
    expect(clarifyButton()).toBeNull();
  });
});
