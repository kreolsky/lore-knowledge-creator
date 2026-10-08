/** ChatComposer — the scroll-to-bottom button and the queued strip share one overlay column. */
// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
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
