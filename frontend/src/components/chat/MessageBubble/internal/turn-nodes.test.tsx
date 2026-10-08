/** Tests for the assembled turn's renderer (turn-nodes) — Lore's components
 * over the dsh node data, the neutral fallback chip for an unrendered kind. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../../../i18n', () => ({
  // Interpolation is spelled out so a composed title is assertable.
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, string>) => vars ? `${key}(${Object.values(vars).join('|')})` : key,
  }),
}));

import { TurnNodes, type TurnNodeLike } from './turn-nodes';

const node = (kind: string, data: unknown, anchorSeq = 1): TurnNodeLike => ({
  key: `${kind}:${anchorSeq}`, kind, anchorSeq, data,
});

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = (ui: ReactNode) => {
  act(() => root.render(ui));
  return container;
};

describe('TurnNodes', () => {
  it('renders assistant text and reasoning blocks in order', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('assistant-step', {
        blocks: [
          { kind: 'reasoning', text: 'thinking…' },
          { kind: 'text', text: 'Answer **bold**' },
        ],
      })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('Answer');
  });

  it('renders a settled tool call with its name and args gist (collapsed body)', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('tool-call', {
        root: {
          kind: 'tool-result', callId: 'c1',
          call: { name: 'search_materials', argsRaw: '{"q":"lore"}' },
          content: [{ type: 'text', text: '3 hits' }], isError: false,
        },
      })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('search_materials');
    expect(html).toContain('q: lore');
  });

  it('renders a settled tool result directly in the plate body, no inner bordered box', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('tool-call', {
        root: {
          kind: 'tool-result', callId: 'c1',
          call: { name: 'search_materials', argsRaw: '{"q":"lore"}' },
          content: [{ type: 'text', text: 'hit one' }],
        },
      })],
      isStreaming: false,
    }));
    act(() => { html.querySelector('button[aria-expanded]')!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    const pre = html.querySelector('pre');
    expect(pre?.textContent).toBe('hit one');
    expect(pre!.className).not.toMatch(/\bborder\b/);
    expect(pre!.className).not.toContain('bg-surface2');
    expect(pre!.className).not.toContain('max-h-');
  });

  it('renders the halt card with its reason (unknown reasons degrade visibly)', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('halt', { turn: 0, reason: 'aborted' })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('chatHaltReasonUnknown');
  });

  it('renders an AUTH turn-error as the translated gateway-refusal sentence above the AUTH chip', () => {
    // dsh blanks the AUTH message deliberately (it can echo credentials); Lore
    // maps the stable code to the actionable sentence at render time.
    const html = render(createElement(TurnNodes, {
      nodes: [node('turn-error', { code: 'AUTH', message: '' })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('chatTurnErrorAuth');
    expect(html).toContain('AUTH');
  });

  it('renders an empty-message turn-error as chatHaltReasonError instead of a blank line', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('turn-error', { code: 'RATE_LIMIT', message: '' })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('chatHaltReasonError');
  });

  it('renders a non-empty turn-error message as today (unchanged path)', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('turn-error', { code: 'RATE_LIMIT', message: 'slow down' })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('slow down');
    expect(html).not.toContain('chatHaltReasonError');
  });

  it('renders an unrendered kind as the neutral fallback chip labelled by kind', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('todo/updated', { items: ['a'] })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('todo/updated');
  });

  it('draws nothing for the message kinds Lore renders itself', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('user', { content: [] }), node('turn-tail', { turn: 0, seq: 1, time: 2, closing: null, branchUnavailable: false })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toBe('');
  });

  it('renders the compaction mint outcome', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('compaction-mint', { turn: 0, compactionEntryId: 'w1', mintFailed: false })],
      isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('chatContextSummarized');
  });

  it('renders the image-gen run as TWO chips — the refined prompt plate and the images plate', () => {
    // Defect D (user smoke 2026-09-07): the settled detached run must keep the
    // two-chip look the old agent_steps path drew — a prompt chip + an images
    // chip — on BOTH paths (live mint and reload mint render the same node).
    const html = render(createElement(TurnNodes, {
      nodes: [node('image-gen', {
        turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref-1'],
        refine: { ok: true, prompt: 'a better prompt' }, title: 'Doc title',
      })],
      isStreaming: false,
    }));
    const text = html.textContent ?? '';
    // The prompt chip: its own plate, titled, collapsed (the old look).
    expect(text).toContain('refinePrompt');
    // The images chip: its own plate with the run's document title.
    expect(text).toContain('generatedImage');
    expect(text).toContain('Doc title');
    // TWO plates, not one flat block: the prompt text lives INSIDE its own
    // collapsible chip (hidden until opened), not below the thumbs.
    const plates = html.querySelectorAll('button[aria-expanded]');
    expect(plates.length).toBe(2);
    expect(text).not.toContain('a better prompt');
  });

  it('renders a failed refine as the failed prompt chip and a failed run loudly', () => {
    const failedRun = render(createElement(TurnNodes, {
      nodes: [node('image-gen', { turn: 0, runId: 'r1', status: 'failed', error: 'queue full' })],
      isStreaming: false,
    })).textContent ?? '';
    // The identity t-mock does not interpolate {error} — the payload-level
    // error assertion lives in conversation-feed.test.ts; here we pin the
    // LOUD failed branch (not the two neutral plates).
    expect(failedRun).toContain('imageGenerationFailed');

    const failedRefine = render(createElement(TurnNodes, {
      nodes: [node('image-gen', {
        turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref-1'],
        refine: { ok: false, error: 'refiner offline' },
      })],
      isStreaming: false,
    })).textContent ?? '';
    expect(failedRefine).toContain('refinePrompt');
  });

  it('renders a RUNNING run as the live phase chip (the run rides the chat channel)', () => {
    // The phases are lore/image-gen RUNNING frames — the node renders the
    // "Generating image — <phase>" chip until the settled frame
    // folds the node into its two-chip (or failed) look.
    const html = render(createElement(TurnNodes, {
      nodes: [node('image-gen', {
        turn: 0, runId: 'r1', status: 'running', phase: 'generating',
      })],
      isStreaming: true,
    }));
    const text = html.textContent ?? '';
    expect(text).toContain('generatingImage');
    expect(text).toContain('genPhaseGenerating');
    // Not the settled look: no refine prompt plate, no thumbs.
    expect(text).not.toContain('refinePrompt');
    expect(text).not.toContain('generatedImage');
    // A header-like row, not a bordered box.
    expect(html.querySelector('.bg-surface2')).toBeNull();
    expect(html.querySelector('.border-border-soft')).toBeNull();

    // No phase known (a running frame without one) — the bare label stands.
    const bare = render(createElement(TurnNodes, {
      nodes: [node('image-gen', { turn: 0, runId: 'r1', status: 'running' })],
      isStreaming: true,
    })).textContent ?? '';
    expect(bare).toContain('generatingImage');
    expect(bare).not.toContain('genPhaseGenerating');
  });
});

describe('TurnNodes — dsh process groups', () => {
  const closed = (counts: { kind: string; count: number }[] = [{ kind: 'tools', count: 2 }], members = 2) => ({
    members, closed: true, summary: { counts, running: undefined, runningDetail: '' },
  }) as unknown as TurnNodeLike['group'];
  const tool = (seq: number, name: string, isError = false): TurnNodeLike => ({
    ...node('tool-call', {
      root: {
        kind: 'tool-result', callId: `c${seq}`, isError,
        call: { name, argsRaw: '{}' }, content: [{ type: 'text', text: `${name} result` }],
      },
    }, seq),
  });
  const inGroup = (n: TurnNodeLike, group: TurnNodeLike['group'], groupKey = 'g1'): TurnNodeLike => ({ ...n, groupKey, group });
  const headers = (html: HTMLElement) => [...html.querySelectorAll('button[aria-expanded]')].map(b => b.textContent);
  const press = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

  it('draws nothing for the turn-process control (Lore has no whole-turn fold)', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('turn-process', { turn: 0, toolCallCount: 2, messageCount: 0 })],
      isStreaming: false,
    }));
    expect(html.textContent).toBe('');
    expect(headers(html)).toEqual([]);
  });

  it('a one-member group renders flat, exactly as without a group', () => {
    const g = closed(undefined, 1);
    const html = render(createElement(TurnNodes, { nodes: [inGroup(tool(1, 'search_materials'), g)], isStreaming: false }));
    expect(headers(html)).toEqual(['search_materials']);
  });

  it('two members fold into ONE collapsed group plate holding both chips', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'search_materials'), g), inGroup(tool(2, 'read_document'), g)],
      isStreaming: false,
    }));
    expect(headers(html)).toEqual(['processGroupDone_tools']);
    press(html.querySelector('button[aria-expanded]')!);
    expect(headers(html)).toEqual(['processGroupDone_tools', 'search_materials', 'read_document']);
  });

  it('a member step keeps its reasoning in the group and draws its reply after the plate', () => {
    const g = closed();
    const step = inGroup(node('assistant-step', {
      blocks: [{ kind: 'reasoning', text: 'thinking it over' }, { kind: 'text', text: 'The answer' }],
    }, 3), g);
    const html = render(createElement(TurnNodes, { nodes: [inGroup(tool(1, 'search_materials'), g), step], isStreaming: false }));
    const text = html.textContent ?? '';
    // Collapsed group: the reasoning is hidden, the reply is not.
    expect(text).toContain('The answer');
    expect(text).not.toContain('reasoningLabel');
    expect(text.indexOf('processGroupDone_tools')).toBeLessThan(text.indexOf('The answer'));
    press(html.querySelector('button[aria-expanded]')!);
    expect(headers(html)).toEqual(['processGroupDone_tools', 'search_materials', 'reasoningLabel']);
    // The reply is drawn once, outside the group.
    expect((html.textContent ?? '').split('The answer').length - 1).toBe(1);
  });

  it('spaces a group off the text above it, not a group opening the answer', () => {
    const g = closed();
    const plateBox = (html: HTMLElement) => html.querySelector('button[aria-expanded]')!.closest('.my-1')!.parentElement!;
    const opening = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), g), inGroup(tool(2, 'b'), g)],
      isStreaming: false,
    }));
    expect(plateBox(opening).classList.contains('pt-5')).toBe(false);

    const mid = render(createElement(TurnNodes, {
      nodes: [
        node('assistant-step', { blocks: [{ kind: 'text', text: 'Intro paragraph' }] }, 1),
        inGroup(tool(2, 'a'), g), inGroup(tool(3, 'b'), g),
      ],
      isStreaming: false,
    }));
    expect(plateBox(mid).classList.contains('pt-5')).toBe(true);
  });

  it('a pending verdict ask stays outside the group; the rest folds', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [
        inGroup(tool(1, 'search_materials'), g),
        inGroup(tool(2, 'read_document'), g),
        inGroup(node('verdict-ask', { callId: 'c9', toolName: 'edit_document' }, 3), g),
      ],
      isStreaming: false,
    }));
    // Folded plate + the pending ask's own card (its header button).
    expect(headers(html)).toEqual(['processGroupDone_tools', 'edit_document']);
    expect(html.textContent).toContain('verdictAllowOnce');
  });

  it('a settled verdict folds into the group like any record', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [
        inGroup(tool(1, 'search_materials'), g),
        inGroup(node('verdict-ask', { callId: 'c1', toolName: 'search_materials' }, 2), g),
      ],
      isStreaming: false,
    }));
    expect(headers(html)).toEqual(['processGroupDone_tools']);
  });

  it('a group left with one foldable member renders flat', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [
        inGroup(tool(1, 'search_materials'), g),
        inGroup(node('verdict-ask', { callId: 'c9', toolName: 'edit_document' }, 2), g),
      ],
      isStreaming: false,
    }));
    expect(headers(html)).toEqual(['search_materials', 'edit_document']);
  });

  it('a failed member tints the collapsed group header', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'search_materials'), g), inGroup(tool(2, 'read_document', true), g)],
      isStreaming: false,
    }));
    const header = html.querySelector('button[aria-expanded]')!;
    expect(header.textContent).toBe('processGroupDone_tools');
    expect(header.querySelector('.text-amber-400')).not.toBeNull();
  });

  it('titles a closed group from its top categories, joined', () => {
    const two = render(createElement(TurnNodes, {
      nodes: [1, 2].map(i => inGroup(tool(i, 't'), closed([{ kind: 'webSearch', count: 2 }, { kind: 'tools', count: 1 }]))),
      isStreaming: false,
    }));
    expect(headers(two)[0]).toBe('processGroupAnd(processGroupDone_webSearch|processGroupDone_tools)');

    const four = render(createElement(TurnNodes, {
      nodes: [1, 2].map(i => inGroup(tool(i, 't'), closed([
        { kind: 'webSearch', count: 3 }, { kind: 'tools', count: 2 }, { kind: 'read', count: 1 }, { kind: 'edit', count: 1 },
      ]))),
      isStreaming: false,
    }));
    expect(headers(four)[0]).toBe(
      'processGroupEtc(processGroupDone_webSearch, processGroupDone_tools, processGroupDone_read)');
  });

  it('a running group is open while it streams and titled by its live activity', () => {
    const live = { members: 2, closed: false, summary: { counts: [], running: 'webSearch', runningDetail: '' } } as unknown as TurnNodeLike['group'];
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), live), inGroup(tool(2, 'b'), live)],
      isStreaming: true,
    }));
    expect(headers(html)).toEqual(['processGroupRunning_webSearch', 'a', 'b']);
  });
});

