/** Tests for unified store-agnostic MessageBubble — note variant alignment/theme,
 * author header, AI agent distinct style, and user-message width cap. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// WHY warm the bundle: MarkdownContent lazily import()s dsh's markdown renderer
// and nothing here awaits it, so under the vmThreads pool the file can end with
// the import in flight and vitest reports an unhandled rejection from its
// closed module runner. Loading it up front settles the import before any test.
beforeAll(async () => {
  await Promise.all([import('../../dsh/lore-markdown'), import('../../dsh/lore-markdown.css')]);
});

// MarkdownContent pulls useAppStore + global t; mock both so render is pure.
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign(
    (selector: (s: unknown) => unknown) => selector({ showToast: vi.fn() }),
    { getState: () => ({ showToast: vi.fn() }), subscribe: () => () => {} },
  ),
}));
// VerdictCard reads decideVerdict from the chat store; the bubble stays
// store-agnostic, so the card's own store call is mocked at module level.
vi.mock('../../store/chat-store', () => ({
  useChatStore: (selector: (s: unknown) => unknown) => selector({ decideVerdict: vi.fn() }),
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({
    // Mirrors i18n/index.ts interpolate(): unknownAuthor → 'unknown', template
    // keys with {placeholders} interpolate from vars, otherwise returns the key
    // (language-agnostic assertions).
    t: (key: string, vars?: Record<string, string | number>) => {
      if (key === 'unknownAuthor') return 'unknown';
      const templates: Record<string, string> = { imageCounter: '{n} / {total}' };
      const tpl = templates[key] ?? key;
      return tpl.replace(/\{(\w+)\}/g, (_, k) => (vars && k in vars ? String(vars[k]) : `{${k}}`));
    },
  }),
}));

import { MessageBubble } from './MessageBubble';
import type { ChatMessage } from '../../types';

function makeMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    message_id: 'm1',
    chat_id: 's1',
    parent_id: null,
    role: 'user',
    content: 'hi',
    created_at: '2026-05-20T14:59:00Z',
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;
let prevRO: unknown;

beforeEach(() => {
  // jsdom lacks ResizeObserver; the user-bubble width-measurement effect uses it.
  prevRO = (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver;
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver = prevRO;
});

describe('unified MessageBubble — user attachment image lightbox (plan user-attachment-image-lightbox)', () => {
  // Attachments are already-resolved data URIs (persisted path — no fetch), passed
  // directly via message.images. Clicking a thumb opens the SAME src-based lightbox
  // as generated images, so nav/counter behave byte-identically. The full image
  // shares the thumbnail's src, so tests scope to the [role="dialog"] portal (the
  // thumbnails live in `container`, the lightbox in a separate body-level portal).
  function renderUserImages(images: string[]) {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai',
        isOwn: true,
        message: makeMsg({ role: 'user', images }),
      }));
    });
  }

  function openAttachThumb(i: number) {
    const btns = container.querySelectorAll('button[aria-label="viewAttachmentImage"]');
    act(() => { (btns[i] as HTMLButtonElement).click(); });
  }

  function dialogImg() {
    return document.body.querySelector<HTMLImageElement>('[role="dialog"] img');
  }

  const A = 'data:image/png;base64,AAAA';
  const B = 'data:image/png;base64,BBBB';

  it('opens the lightbox showing the clicked attachment\'s data URI as the full image', () => {
    renderUserImages([A]);
    openAttachThumb(0);
    expect(dialogImg()).not.toBeNull();
    expect(dialogImg()!.getAttribute('src')).toBe(A);
  });

  it('count=2: opens at the clicked thumb\'s index', () => {
    renderUserImages([A, B]);
    openAttachThumb(1);
    expect(dialogImg()!.getAttribute('src')).toBe(B);
  });
});

describe('unified MessageBubble — verdict card on a frameless row (render gate)', () => {
  // A row the driver's log has no turn for carries no nodes, so the held call
  // renders from the row's `pending_verdicts` (restored by GET /chat/verdicts).
  // Store-side recording (awaiting_verdict / fetchPendingVerdicts) is bound in
  // streaming.tool-prepare.test.ts and agent-slice.test.ts; these bind the
  // RENDER gate.
  const PENDING = { call_id: 'c1', tool_name: 'create_document', message_id: 'm1' };

  function renderAgentMsg(overrides: Partial<ChatMessage> = {}) {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai',
        isOwn: false,
        isStreaming: true,
        message: makeMsg({ role: 'assistant', content: '', ...overrides }),
      }));
    });
  }

  /** One VerdictCard renders exactly one verdictBody line. */
  function verdictCardCount(): number {
    return (container.textContent ?? '').match(/verdictBody/g)?.length ?? 0;
  }

  it('renders one card per held call', () => {
    renderAgentMsg({
      pending_verdicts: [
        PENDING,
        { call_id: 'c2', tool_name: 'edit_document', message_id: 'm1' },
      ],
    });
    expect(verdictCardCount()).toBe(2);
  });

  it('renders no verdict card when there are no holds', () => {
    renderAgentMsg({});
    expect(verdictCardCount()).toBe(0);
  });
});

describe('unified MessageBubble — the verdict-ask node while the call is still held', () => {
  // dsh appends `tool/call` BEFORE it asks for approval, so by the time the
  // lore/verdict-ask node arrives the call's own tool-call node already exists
  // in the timeline — still RUNNING (root has no `kind`). The decision card
  // must stay, buttons included, until that node carries a tool-result.
  const RUNNING_CALL = { key: 'tc1', kind: 'tool-call', anchorSeq: 10,
    data: { root: { callId: 'c1', name: 'edit_document', argsRaw: '{}' } } };
  const SETTLED_CALL = { key: 'tc1', kind: 'tool-call', anchorSeq: 10,
    data: { root: { kind: 'tool-result', callId: 'c1', call: { name: 'edit_document', argsRaw: '{}' }, content: [] } } };
  const ASK = { key: 'va1', kind: 'verdict-ask', anchorSeq: 11.5,
    data: { turn: 1, callId: 'c1', toolName: 'edit_document' } };

  function renderNodes(nodes: unknown[]) {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai', isOwn: false, isStreaming: true,
        message: makeMsg({ role: 'assistant', content: '' }),
        nodes: nodes as never,
      }));
    });
    return container.textContent ?? '';
  }

  it('renders the decision buttons while the held call has no result yet', () => {
    const text = renderNodes([RUNNING_CALL, ASK]);
    expect(text).toContain('verdictBody');
    expect(text).toContain('verdictAllowOnce');
    expect(text).toContain('verdictReject');
  });

  it('drops the buttons once the call settled (verdict published, call ran)', () => {
    const text = renderNodes([SETTLED_CALL, ASK]);
    expect(text).toContain('edit_document');
    expect(text).not.toContain('verdictAllowOnce');
    expect(text).not.toContain('verdictReject');
  });
});

describe('unified MessageBubble — the row halt column (retire-the-stored-turn-timeline step 1)', () => {
  // The abnormal-end halt card is its own `halt` column. MessageBubble renders
  // it on a row that carries no nodes (the persisted abnormal row shape),
  // alongside the content; a turn WITH nodes carries its own lore/halt card.
  function renderRowHalt(message: Partial<ChatMessage>) {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai',
        isOwn: false,
        message: makeMsg({ role: 'assistant', content: 'partial answer', ...message }),
      }));
    });
  }

  it('labels a BACKEND abnormal reason — the card draws two vocabularies', () => {
    // messages.halt carries the backend's abnormal set (error, turn_timeout,
    // line_unreachable, stream_failed), which the live HaltReason union does
    // not contain. Before the fix these had no arm and rendered as a crash.
    renderRowHalt({ halt: { reason: 'turn_timeout' } });
    expect(container.textContent).toContain('chatHaltReasonTurnTimeout');
  });

  it('names an unknown reason instead of rendering nothing', () => {
    renderRowHalt({ halt: { reason: 'a_reason_added_later' } });
    expect(container.textContent).toContain('chatHaltReasonUnknown');
  });

});

describe('unified MessageBubble — the thinking line while the relay shows nothing', () => {
  // The relay hides dsh's lifecycle and bookkeeping kinds, so the gap between a
  // settled tool and the next step carries no frame: without the line the
  // bubble reads as a finished answer mid-turn.
  function renderStreaming(message: Partial<ChatMessage>) {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai', isOwn: false, isStreaming: true,
        message: makeMsg({ role: 'assistant', content: '', ...message }),
      }));
    });
    return container.textContent ?? '';
  }

  it('stays out of the way while text streams', () => {
    expect(renderStreaming({ content: 'the answer so far' })).not.toContain('thinking');
  });
});
