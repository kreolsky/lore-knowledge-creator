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
});
