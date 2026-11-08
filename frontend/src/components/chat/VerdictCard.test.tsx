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

const chatState = {
  decideVerdict: vi.fn(),
  activeSessionId: null as string | null,
  sessions: [] as Array<{ session_id: string; document_id: string | null }>,
};
vi.mock('../../store/chat-store', () => ({
  useChatStore: (sel: (s: unknown) => unknown) => sel(chatState),
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
  chatState.decideVerdict.mockClear();
  chatState.activeSessionId = null;
  chatState.sessions = [];
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

  // The memory-capture tools' args must render as prose a person can moderate
  // (fact title/body, merge label, anchor fact and targets, the clear line).
  // TEST-WHY: pure-logic-edge — a raw JSON dump is unreadable moderation.
  it('renders save_chat_facts and set_fact_anchor args as prose, never JSON', () => {
    mount({ ...PENDING, tool_name: 'save_chat_facts' }, JSON.stringify({
      verdicts: [
        { action: 'new', title: 'Аспирин: антиагрегант', text: 'Назначается при риске нарушения функции плаценты.' },
        { action: 'merge', fact_id: 'f1', title: null, text: 'Дополняет уже сохранённый факт.' },
      ],
    }));
    let text = container.textContent ?? '';
    expect(text).toContain('Аспирин: антиагрегант');
    expect(text).toContain('Назначается при риске нарушения функции плаценты.');
    expect(text).toContain('verdictFactMerge');
    expect(text).not.toContain('verdictFactSupersede');
    expect(container.querySelector('pre')).toBeNull();

    mount({ ...PENDING, tool_name: 'set_fact_anchor' }, JSON.stringify({
      fact_id: 'fact:9', document_ids: ['docA', 'docB'],
    }));
    text = container.textContent ?? '';
    expect(text).toContain('fact:9');
    expect(text).toContain('docA, docB');
    expect(text).not.toContain('verdictAnchorClear');
    expect(container.querySelector('pre')).toBeNull();

    // document_ids: [] is the CLEAR call — named as such, not an empty list.
    mount({ ...PENDING, tool_name: 'set_fact_anchor' }, JSON.stringify({
      fact_id: 'fact:9', document_ids: [],
    }));
    expect((container.textContent ?? '')).toContain('verdictAnchorClear');
    expect(container.querySelector('pre')).toBeNull();
  });

  // A save from a chat with no parent document parks the facts project-wide
  // only; the warning line must fire exactly there.
  // TEST-WHY: pure-logic-edge — no warning on a bound chat or another tool.
  it('warns about a parentless chat only for save_chat_facts without a document', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: null }];

    mount({ ...PENDING, tool_name: 'save_chat_facts' }, '{"verdicts":[]}');
    expect(container.textContent).toContain('verdictSaveNoParent');

    chatState.sessions = [{ session_id: 's1', document_id: 'docA' }];
    mount({ ...PENDING, tool_name: 'save_chat_facts' }, '{"verdicts":[]}');
    expect(container.textContent).not.toContain('verdictSaveNoParent');

    chatState.sessions = [{ session_id: 's1', document_id: null }];
    mount(PENDING, '{"document_id":"d1","edits":[]}');
    expect(container.textContent).not.toContain('verdictSaveNoParent');
  });
});
