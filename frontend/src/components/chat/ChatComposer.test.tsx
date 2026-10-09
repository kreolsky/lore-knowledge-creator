/** ChatComposer — the scroll-to-bottom/queue overlay column, and the Escape cancel order (voice, then the turn). */
// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { createPortal } from 'react-dom';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { ChatComposer } from './ChatComposer';

function mount(extra: Record<string, unknown>) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(createElement(ChatComposer, {
    variant: 'ai',
    value: '',
    onChange: () => {},
    onSend: () => {},
    isStreaming: false,
    canSend: false,
    placeholder: '',
    recording: false,
    transcribing: false,
    onToggleRecording: () => {},
    images: [],
    onRemoveImage: () => {},
    textareaRef: createRef<HTMLTextAreaElement>(),
    ...extra,
  })));
  return { host, unmount: () => { act(() => root.unmount()); host.remove(); } };
}

const button = (host: HTMLElement) => host.querySelector('button[aria-label="scrollToBottom"]');

describe('ChatComposer — scroll-to-bottom button', () => {
  it('is absent without a handler', () => {
    const { host, unmount } = mount({});
    expect(button(host)).toBeNull();
    unmount();
  });

  it('renders and calls the handler on click', () => {
    const onScrollToBottom = vi.fn();
    const { host, unmount } = mount({ onScrollToBottom });
    act(() => (button(host) as HTMLButtonElement).click());
    expect(onScrollToBottom).toHaveBeenCalledTimes(1);
    unmount();
  });

  it('sits in the same column ABOVE the queued plates, so the queue lifts it', () => {
    const { host, unmount } = mount({ onScrollToBottom: () => {}, queued: ['later'], onRemoveQueued: () => {} });
    const btn = button(host)!;
    const plate = host.querySelector('[title="queuedMessageTitle"]')!;
    const column = btn.parentElement!;
    expect(column.contains(plate)).toBe(true);
    expect(btn.compareDocumentPosition(plate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    unmount();
  });
});

describe('ChatComposer — Escape cancels one thing per press, voice before the turn', () => {
  const press = (target: Element, init: KeyboardEventInit = {}) => {
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true, ...init });
    act(() => { target.dispatchEvent(ev); });
    return ev;
  };
  const mic = (host: HTMLElement) => host.querySelector('.mic-btn-wrap button')!;

  it('recording + streaming: Escape on the mic button discards the recording, the turn keeps going', () => {
    const onCancelRecording = vi.fn();
    const onStop = vi.fn();
    const { host, unmount } = mount({ recording: true, isStreaming: true, onStop, onCancelRecording });
    const ev = press(mic(host));
    expect(onCancelRecording).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(true);
    unmount();
  });

  it('streaming without recording: Escape in the textarea stops the turn', () => {
    const onCancelRecording = vi.fn();
    const onStop = vi.fn();
    const { host, unmount } = mount({ isStreaming: true, onStop, onCancelRecording });
    press(host.querySelector('textarea')!);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onCancelRecording).not.toHaveBeenCalled();
    unmount();
  });

  it('streaming without recording: Escape on the mic button also stops the turn', () => {
    const onStop = vi.fn();
    const { host, unmount } = mount({ isStreaming: true, onStop, onCancelRecording: vi.fn() });
    press(mic(host));
    expect(onStop).toHaveBeenCalledTimes(1);
    unmount();
  });

  it('idle: Escape does nothing and leaves the event alone', () => {
    const onCancelRecording = vi.fn();
    const onStop = vi.fn();
    const { host, unmount } = mount({ onStop, onCancelRecording });
    const ev = press(host.querySelector('textarea')!);
    expect(onStop).not.toHaveBeenCalled();
    expect(onCancelRecording).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(false);
    unmount();
  });

  it('an Escape already handled inside the composer (popover) is not acted on again', () => {
    const onStop = vi.fn();
    const { host, unmount } = mount({ isStreaming: true, onStop, onCancelRecording: vi.fn() });
    const ta = host.querySelector('textarea')!;
    const swallow = (e: Event) => e.preventDefault();
    ta.addEventListener('keydown', swallow);
    press(ta);
    expect(onStop).not.toHaveBeenCalled();
    ta.removeEventListener('keydown', swallow);
    unmount();
  });

  it('an Escape from a portaled child outside the composer DOM is ignored', () => {
    const onStop = vi.fn();
    const outside = document.createElement('div');
    document.body.appendChild(outside);
    const Portaled = () => createPortal(createElement('input', { 'data-testid': 'portaled' }), outside);
    const { unmount } = mount({ isStreaming: true, onStop, onCancelRecording: vi.fn(), topControls: createElement(Portaled) });
    press(outside.querySelector('input')!);
    expect(onStop).not.toHaveBeenCalled();
    unmount();
    outside.remove();
  });
});
