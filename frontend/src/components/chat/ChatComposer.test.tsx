/** ChatComposer — the scroll-to-bottom/queue overlay column, and the Escape cancel order (voice, then the turn). */
// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { ChatComposer } from './ChatComposer';
import { useUIStore } from '../../store/ui-store';

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

describe('ChatComposer — compact (phone) layout', () => {
  afterEach(() => useUIStore.setState({ compactLayout: false }));

  it('wide: drag-resizer present, Stop carries its label', () => {
    useUIStore.setState({ compactLayout: false });
    const { host, unmount } = mount({ isStreaming: true, onStop: () => {} });
    expect(host.querySelector('.resizer-v')).not.toBeNull();
    expect(host.querySelector('textarea')?.getAttribute('rows')).toBeNull();
    const stop = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('stop'));
    expect(stop).toBeDefined();
    unmount();
  });

  it('compact: no drag-resizer, a two-row textarea, Stop is an icon-only square', () => {
    useUIStore.setState({ compactLayout: true });
    const onStop = vi.fn();
    const { host, unmount } = mount({ isStreaming: true, onStop });
    expect(host.querySelector('.resizer-v')).toBeNull();
    expect(host.querySelector('textarea')?.getAttribute('rows')).toBe('2');
    const stop = host.querySelector('button[title="stop"]') as HTMLButtonElement;
    expect(stop.textContent).toBe('');
    act(() => stop.click());
    expect(onStop).toHaveBeenCalledTimes(1);
    unmount();
  });
});
