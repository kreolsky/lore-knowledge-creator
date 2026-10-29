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
