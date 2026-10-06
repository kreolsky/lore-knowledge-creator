/** Tests for the assembled turn's renderer (turn-nodes) — Lore's components
 * over the dsh node data, the neutral fallback chip for an unrendered kind. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../../../i18n', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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

    // No phase known (a running frame without one) — the bare label stands.
    const bare = render(createElement(TurnNodes, {
      nodes: [node('image-gen', { turn: 0, runId: 'r1', status: 'running' })],
      isStreaming: true,
    })).textContent ?? '';
    expect(bare).toContain('generatingImage');
    expect(bare).not.toContain('genPhaseGenerating');
  });
});
