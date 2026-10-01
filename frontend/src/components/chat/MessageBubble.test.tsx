/** Tests for unified store-agnostic MessageBubble — note variant alignment/theme,
 * author header, AI agent distinct style, and user-message width cap. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
import { userColor } from '../../utils/user-color';
import type { ChatMessage, HaltReason } from '../../types';

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

/** Returns the outermost bubble wrapper (the `flex flex-col` alignment container). */
function wrapper() {
  return container.firstElementChild as HTMLElement;
}

/** jsdom normalizes hex colors to rgb(...) when reading back inline styles. */
function toRgb(hex: string): string {
  const probe = document.createElement('div');
  probe.style.borderLeftColor = hex;
  return probe.style.borderLeftColor;
}

/** Returns the bubble body div — the first child after the author header. */
function bubbleBody() {
  // The header (when present) is text-text-dim; the body holds the bg tokens.
  const divs = Array.from(wrapper().children) as HTMLElement[];
  return divs.find(d => !d.className.includes('text-text-dim') && !d.className.includes('mt-0.5')) ?? divs[0];
}

describe('unified MessageBubble — note variant', () => {
  it('right-aligns (items-end) and uses sticky-yellow for own messages', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg({ author_name: 'red' }),
        isOwn: true,
        actions: { onCopy: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn() },
      }));
    });
    expect(wrapper().className).toContain('items-end');
    expect(bubbleBody().className).toContain('sticky-yellow-dark');
  });

  it('left-aligns (items-start) and renders a transparent bubble with the author-color left border for other-user messages', () => {
    const authorId = 'user-blue-123';
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg({ author_name: 'blue', author_id: authorId }),
        isOwn: false,
        actions: { onCopy: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn() },
      }));
    });
    expect(wrapper().className).toContain('items-start');
    // Fully transparent plaque: no surface fill (sticky-yellow is own-only).
    expect(bubbleBody().className).not.toContain('bg-surface2');
    expect(bubbleBody().className).not.toContain('sticky-yellow-dark');
    // 4px author-colored left border at 80% transparency; no rounding (project rule).
    const color = `${userColor(authorId)}33`;
    expect(bubbleBody().style.borderLeftWidth).toBe('4px');
    expect(bubbleBody().style.borderLeftStyle).toBe('solid');
    expect(bubbleBody().style.borderLeftColor).toBe(toRgb(color));
    expect(bubbleBody().style.borderRadius).toBe('');
  });

  it('keeps the sticky-yellow plaque and has no colored border for own note messages', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg({ author_name: 'red', author_id: 'user-red-9' }),
        isOwn: true,
        actions: { onCopy: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn() },
      }));
    });
    expect(bubbleBody().className).toContain('sticky-yellow-dark');
    expect(bubbleBody().style.borderLeft).toBe('');
  });

  it('omits the colored border when an other-user note message has no author_id (defensive guard)', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg({ author_name: 'blue' }),
        isOwn: false,
        actions: { onCopy: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn() },
      }));
    });
    expect(bubbleBody().style.borderLeft).toBe('');
  });

  it('renders "<author_name> | <formatted date>" header for a user-authored message', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg({ author_name: 'red' }),
        isOwn: true,
      }));
    });
    const header = container.querySelector('.text-text-dim');
    expect(header?.textContent).toMatch(/^red \| 2026-05-20 \d{2}:\d{2}$/);
  });

  it('renders "system notes | <date>" for pipeline-authored messages', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg({ author_name: 'system notes' }),
        isOwn: false,
      }));
    });
    const header = container.querySelector('.text-text-dim');
    expect(header?.textContent).toMatch(/^system notes \| 2026-05-20 \d{2}:\d{2}$/);
  });

  it('falls back to the unknownAuthor i18n key when author_name is missing', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note',
        message: makeMsg(),
        isOwn: true,
      }));
    });
    const header = container.querySelector('.text-text-dim');
    expect(header?.textContent).toMatch(/^unknown \| 2026-05-20 \d{2}:\d{2}$/);
  });
});

describe('unified MessageBubble — AI variant', () => {
  it('renders agent/assistant messages full-width with the base AI style (no outer card)', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai',
        message: makeMsg({ role: 'assistant', content: 'response' }),
        isOwn: false,
      }));
    });
    const body = bubbleBody();
    expect(body.className).toContain('w-full');
    // No outer surface card: reasoning/sources/tool chips carry their own cards,
    // so an outer bg-surface2/border would nest identical plates (rejected base).
    expect(body.className).toContain('px-0');
    expect(body.className).not.toContain('bg-surface2');
    expect(body.className).not.toContain('border');
  });

  it('caps user-message width at 85% and aligns own messages right', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai',
        message: makeMsg({ role: 'user' }),
        isOwn: true,
      }));
    });
    expect(bubbleBody().className).toContain('max-w-[85%]');
    expect(wrapper().className).toContain('items-end');
  });

  it('hides fork/regenerate actions when their callbacks are absent (note-like minimal actions)', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai',
        message: makeMsg({ role: 'user' }),
        isOwn: true,
        actions: { onCopy: vi.fn() },
      }));
    });
    // Only copy present → no fork/regenerate/edit/delete buttons (icons absent).
    const titles = Array.from(container.querySelectorAll('[title]')).map(b => b.getAttribute('title'));
    expect(titles).toContain('copy');
    expect(titles).not.toContain('forkAndResend');
    expect(titles).not.toContain('regenerate');
  });
});

describe('unified MessageBubble — agent edit→fork', () => {
  /** Find an action button (IconButton) by its title attribute. */
  function buttonByTitle(title: string): HTMLElement {
    const el = container.querySelector(`[title="${title}"]`) as HTMLElement | null;
    if (!el) throw new Error(`no button with title=${title}`);
    return el;
  }

  /** Set a React-controlled textarea's value + dispatch input (jsdom-safe). */
  function setTextarea(text: string) {
    const ta = container.querySelector('textarea') as HTMLTextAreaElement;
    if (!ta) throw new Error('no textarea (not in edit mode)');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
    act(() => {
      setter.call(ta, text);
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  it('shows an Edit button (not a fork button) for an agent user message', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai', isAgent: true, isOwn: true,
        message: makeMsg({ role: 'user' }),
        actions: { onCopy: vi.fn(), onForkResend: vi.fn(), onEdit: vi.fn() },
      }));
    });
    const titles = Array.from(container.querySelectorAll('[title]')).map(b => b.getAttribute('title'));
    expect(titles).toContain('edit');            // Edit button present (agent edit→fork)
    expect(titles).not.toContain('forkAndResend'); // no separate fork button for agent
  });

  it('does NOT show an Edit button on an agent (assistant) reply — editing replies is unsupported', () => {
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai', isAgent: true, isOwn: false,
        message: makeMsg({ role: 'assistant', message_id: 'a1' }),
        actions: { onCopy: vi.fn(), onForkResend: vi.fn(), onEdit: vi.fn() },
      }));
    });
    const titles = Array.from(container.querySelectorAll('[title]')).map(b => b.getAttribute('title'));
    expect(titles).not.toContain('edit');          // agent replies are not editable
    expect(titles).not.toContain('forkAndResend');
  });

  it('Save in an agent chat FORKS (not in-place edit)', async () => {
    const onForkResend = vi.fn();
    const onEdit = vi.fn();
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai', isAgent: true, isOwn: true,
        message: makeMsg({ role: 'user', content: 'original' }),
        actions: { onCopy: vi.fn(), onForkResend, onEdit },
      }));
    });
    await act(async () => { buttonByTitle('edit').click(); });   // open edit mode
    await act(async () => { buttonByTitle('save').click(); });   // save → fork (async)
    expect(onForkResend).toHaveBeenCalledWith('m1', 'original', undefined);
    expect(onEdit).not.toHaveBeenCalled();                       // in-place edit NOT invoked
  });

  it('Save forks with the EDITED text', async () => {
    const onForkResend = vi.fn();
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'ai', isAgent: true, isOwn: true,
        message: makeMsg({ role: 'user', content: 'original' }),
        actions: { onCopy: vi.fn(), onForkResend, onEdit: vi.fn() },
      }));
    });
    await act(async () => { buttonByTitle('edit').click(); });
    setTextarea('edited prompt');
    await act(async () => { buttonByTitle('save').click(); });
    expect(onForkResend).toHaveBeenCalledWith('m1', 'edited prompt', undefined);
  });

  it('Save in a NOTE chat is in-place (onEdit), not a fork', () => {
    const onForkResend = vi.fn();
    const onEdit = vi.fn();
    act(() => {
      root.render(createElement(MessageBubble, {
        variant: 'note', isOwn: true,
        message: makeMsg({ role: 'user', content: 'note text' }),
        actions: { onCopy: vi.fn(), onEdit, onForkResend, onDelete: vi.fn() },
      }));
    });
    act(() => { buttonByTitle('edit').click(); });
    setTextarea('edited note');
    act(() => { buttonByTitle('save').click(); });
    expect(onEdit).toHaveBeenCalledWith('m1', 'edited note');
    expect(onForkResend).not.toHaveBeenCalled();
  });
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

  function dialog() {
    return document.body.querySelector('[role="dialog"]') as HTMLElement;
  }

  function dialogImg() {
    return document.body.querySelector<HTMLImageElement>('[role="dialog"] img');
  }

  function pressKey(key: string) {
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key })); });
  }

  const A = 'data:image/png;base64,AAAA';
  const B = 'data:image/png;base64,BBBB';

  it('renders one clickable thumbnail per attachment (no bare <img>)', () => {
    renderUserImages([A, B]);
    const btns = container.querySelectorAll('button[aria-label="viewAttachmentImage"]');
    expect(btns.length).toBe(2);
    const imgs = Array.from(btns).map(b => b.querySelector('img')!.getAttribute('src'));
    expect(imgs).toEqual([A, B]);
  });

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

  it('count=2: ArrowRight advances to the next image', () => {
    renderUserImages([A, B]);
    openAttachThumb(0);
    pressKey('ArrowRight');
    expect(dialogImg()!.getAttribute('src')).toBe(B);
  });

  it('count=2: ArrowRight wraps from last back to first', () => {
    renderUserImages([A, B]);
    openAttachThumb(1);
    pressKey('ArrowRight');
    expect(dialogImg()!.getAttribute('src')).toBe(A);
  });

  it('count=2: ArrowLeft wraps from first back to last', () => {
    renderUserImages([A, B]);
    openAttachThumb(0);
    pressKey('ArrowLeft');
    expect(dialogImg()!.getAttribute('src')).toBe(B);
  });

  it('count=2: the right-third nextImage nav zone advances', () => {
    renderUserImages([A, B]);
    openAttachThumb(0);
    const next = dialog().querySelector<HTMLButtonElement>('button[aria-label="nextImage"]');
    expect(next).not.toBeNull();
    act(() => { next!.click(); });
    expect(dialogImg()!.getAttribute('src')).toBe(B);
  });

  it('count=2: counter shows n/total and updates on navigation', () => {
    renderUserImages([A, B]);
    openAttachThumb(0);
    expect(dialog().textContent).toContain('1 / 2');
    pressKey('ArrowRight');
    expect(dialog().textContent).toContain('2 / 2');
  });

  it('single attachment: no nav zones, no counter, ArrowRight is a no-op', () => {
    renderUserImages([A]);
    openAttachThumb(0);
    const dlg = dialog();
    expect(dlg.querySelectorAll('button[aria-label="previousImage"]')).toHaveLength(0);
    expect(dlg.querySelectorAll('button[aria-label="nextImage"]')).toHaveLength(0);
    expect(dlg.textContent).not.toContain('1 / 1');
    pressKey('ArrowRight');
    expect(dialogImg()!.getAttribute('src')).toBe(A);
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

  it('renders the verdict card on a frameless row (tool-first turn)', () => {
    renderAgentMsg({ pending_verdicts: [PENDING] });
    expect(container.textContent).toContain('verdictBody');
    expect(container.textContent).toContain('verdictAllowOnce');
    expect(container.textContent).toContain('create_document');
  });

  it('renders the verdict card for a reloaded message (holds from the REST list, no live frames)', () => {
    // Reload shape: persisted content, pending_verdicts merged onto the
    // message by fetchPendingVerdicts (GET /api/chat/verdicts).
    renderAgentMsg({ content: 'partial reply', pending_verdicts: [PENDING] });
    expect(verdictCardCount()).toBe(1);
  });

  it('renders one card per held call', () => {
    renderAgentMsg({
      pending_verdicts: [
        PENDING,
        { call_id: 'c2', tool_name: 'edit_document', message_id: 'm1' },
      ],
    });
    expect(verdictCardCount()).toBe(2);
  });

  it('a resolved hold removes the card (pending_verdicts filtered by the store)', () => {
    renderAgentMsg({ content: 'created d1', pending_verdicts: [] });
    expect(verdictCardCount()).toBe(0);
    expect(container.textContent).toContain('created d1');
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

  it('renders the halt card + the content for a frameless row with a halt column', () => {
    renderRowHalt({ halt: { reason: 'disconnected', steps: 1 } });
    expect(container.textContent).toContain('chatHaltReasonDisconnected');
    expect(container.textContent).toContain('chatHaltStepCountNoLimit');
    expect(container.textContent).toContain('partial answer');
  });

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

  it('shows while the turn has produced no text yet', () => {
    expect(renderStreaming({})).toContain('thinking');
  });

  it('stays out of the way while text streams', () => {
    expect(renderStreaming({ content: 'the answer so far' })).not.toContain('thinking');
  });
});
