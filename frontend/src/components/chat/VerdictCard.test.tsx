/** VerdictCard — the mid-turn approval decision card: three actions (allow
 * once / allow for the session / reject with text) plus rendering from
 * `pending_verdicts` (the GET /api/chat/verdicts reload re-render path feeds
 * the same shape). */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const decideVerdict = vi.fn();
vi.mock('../../store/chat-store', () => ({
  useChatStore: (sel: (s: unknown) => unknown) => sel({ decideVerdict }),
}));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { VerdictCard } from './VerdictCard';
import type { PendingVerdict } from '../../types';

let container: HTMLDivElement;
let root: Root;

function mount(pending: PendingVerdict, argsRaw?: string) {
  act(() => { root.render(createElement(VerdictCard, { pending, argsRaw })); });
}

function buttons(): HTMLButtonElement[] {
  return [...container.querySelectorAll('button')] as HTMLButtonElement[];
}

function clickButtonByText(text: string) {
  const btn = buttons().find(b => b.textContent?.trim() === text);
  if (!btn) throw new Error(`no button with text "${text}"`);
  act(() => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); });
}

/** Type into the reason input the way a browser would — the native value setter
 * bypasses React's value tracker so the synthetic input event reads as a change
 * (a direct `input.value = …` is tracked by React and the change is ignored). */
function typeReason(value: string) {
  const input = container.querySelector('input');
  if (!input) throw new Error('no reason input rendered');
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

const PENDING: PendingVerdict = {
  call_id: 'c1', tool_name: 'edit_document', message_id: 'am',
};

beforeEach(() => {
  decideVerdict.mockClear();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

describe('VerdictCard', () => {
  it('renders the held tool name and the verdict body from pending_verdicts', () => {
    mount(PENDING);
    const text = container.textContent ?? '';
    expect(text).toContain('edit_document');
    expect(text).toContain('verdictBody');
    expect(text).toContain('verdictAllowOnce');
    expect(text).toContain('verdictAllowSession');
    expect(text).toContain('verdictReject');
  });

  it('allow once publishes the allow_once action', async () => {
    mount(PENDING);
    clickButtonByText('verdictAllowOnce');
    await act(async () => {});
    expect(decideVerdict).toHaveBeenCalledWith('c1', 'allow_once', 'edit_document', undefined);
  });

  it('allow session publishes the allow_session action', async () => {
    mount(PENDING);
    clickButtonByText('verdictAllowSession');
    await act(async () => {});
    expect(decideVerdict).toHaveBeenCalledWith('c1', 'allow_session', 'edit_document', undefined);
  });

  it('reject with text enters the reason input then publishes reject', async () => {
    mount(PENDING);
    clickButtonByText('verdictReject');
    expect(container.querySelector('input')).not.toBeNull();
    typeReason('do not touch that');
    clickButtonByText('verdictReject');
    await act(async () => {});
    expect(decideVerdict).toHaveBeenCalledWith('c1', 'reject', 'edit_document', 'do not touch that');
  });

  it('reject stays disabled while a verdict is in flight (no double publish)', async () => {
    let resolve: (v: unknown) => void = () => {};
    decideVerdict.mockReturnValueOnce(new Promise(r => { resolve = r; }));
    mount(PENDING);
    clickButtonByText('verdictAllowOnce');
    // Only the three decision actions carry disabled — the ToolPlate header
    // toggle button never does.
    const actionLabels = ['verdictAllowOnce', 'verdictAllowSession', 'verdictReject'];
    const actionButtons = buttons().filter(b => actionLabels.includes(b.textContent?.trim() ?? ''));
    expect(actionButtons).toHaveLength(3);
    expect(actionButtons.every(b => b.hasAttribute('disabled'))).toBe(true);
    await act(async () => { resolve(null); });
    expect(decideVerdict).toHaveBeenCalledTimes(1);
  });
});

describe('VerdictCard — proposed change preview', () => {
  it('renders each edit_document edit as a removed/added pair above the verdict body', () => {
    mount(PENDING, JSON.stringify({
      document_id: 'd1',
      edits: [
        { old_string: 'old line', new_string: 'new line' },
        { old_string: 'second old', new_string: 'second new' },
      ],
    }));
    const text = container.textContent ?? '';
    expect(text).toContain('old line');
    expect(text).toContain('new line');
    expect(text).toContain('second old');
    expect(text).toContain('second new');
    // Removed BEFORE added, per pair.
    expect(text.indexOf('old line')).toBeLessThan(text.indexOf('new line'));
    expect(text).toContain('verdictBody');
    const pres = [...container.querySelectorAll('pre')];
    expect(pres.length).toBe(4);
    expect(pres[0].className).toContain('text-red');
    expect(pres[1].className).toContain('text-green');
    // A huge new_string cannot blow up the chat column.
    expect(pres.every(p => p.className.includes('max-h-'))).toBe(true);
  });

  it('renders each edit_table_cell edit labelled by its column and row', () => {
    mount({ ...PENDING, tool_name: 'edit_table_cell' }, JSON.stringify({
      edits: [{ table_id: 't1', row: 2, column: 'Status', old_value: 'todo', new_value: 'done' }],
    }));
    const text = container.textContent ?? '';
    expect(text).toContain('Status · 2');
    expect(text.indexOf('todo')).toBeLessThan(text.indexOf('done'));
    const pres = [...container.querySelectorAll('pre')];
    expect(pres.length).toBe(2);
    expect(pres[0].textContent).toBe('todo');
    expect(pres[0].className).toContain('text-red');
    expect(pres[1].textContent).toBe('done');
    expect(pres[1].className).toContain('text-green');
  });

  it('renders any other tool\'s args as pretty-printed JSON', () => {
    mount({ ...PENDING, tool_name: 'create_document' }, '{"title":"Notes","parent_id":null}');
    const pre = container.querySelector('pre');
    expect(pre?.textContent).toBe(JSON.stringify({ title: 'Notes', parent_id: null }, null, 2));
  });

  it('renders unparsable args as the raw string', () => {
    mount(PENDING, '{not json');
    expect(container.querySelector('pre')?.textContent).toBe('{not json');
  });

  it('renders no args block when no args are known (the reload path keeps today\'s card)', () => {
    mount(PENDING);
    expect(container.querySelector('pre')).toBeNull();
    expect(container.textContent).toContain('verdictBody');
  });
});
