/** Tests for the assembled turn's renderer (turn-nodes) — Lore's components
 * over the dsh node data, the neutral fallback chip for an unrendered kind. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// WHY warm the bundle (MessageBubble.test.tsx precedent): MarkdownContent
// lazily import()s dsh's markdown renderer and the assistant-step tests render
// it; under the vmThreads pool the file can end with the import in flight and
// vitest reports an unhandled rejection from its closed module runner.
beforeAll(async () => {
  await Promise.all([import('../../../../dsh/lore-markdown'), import('../../../../dsh/lore-markdown.css')]);
});

vi.mock('../../../../i18n', () => ({
  // Interpolation is spelled out so a composed title is assertable.
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, string>) => vars ? `${key}(${Object.values(vars).join('|')})` : key,
  }),
}));

// The image-delete gate reads accessLevel/isPublicShare and calls the delete
// API + toast through the stores' getState — mutable per-test state behind the
// selector mocks (real modules otherwise; nothing else in this graph changes).
const appState = vi.hoisted(() => ({ accessLevel: 'full' as string, showToast: vi.fn() }));
vi.mock('../../../../store/app-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../../store/app-store')>();
  const useAppStore = Object.assign(
    (selector: (s: typeof appState) => unknown) => selector(appState),
    {
      getState: () => appState,
      // chat-store module-level store-to-store subscription needs a no-op.
      subscribe: () => () => {},
    },
  );
  return { ...actual, useAppStore };
});
const uiState = vi.hoisted(() => ({ isPublicShare: false }));
vi.mock('../../../../store/ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../../store/ui-store')>();
  return { ...actual, useUIStore: (selector: (s: typeof uiState) => unknown) => selector(uiState) };
});
const apiMocks = vi.hoisted(() => ({ delete: vi.fn() }));
vi.mock('../../../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../../api/client')>();
  return { ...actual, apiClient: { ...actual.apiClient, delete: apiMocks.delete } };
});

import { TurnNodes, type TurnNodeLike } from './turn-nodes';
import { useDeletedRefIds } from '../../../../store/deleted-ref-ids';

const node = (kind: string, data: unknown, anchorSeq = 1): TurnNodeLike => ({
  key: `${kind}:${anchorSeq}`, kind, anchorSeq, data,
});

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  appState.accessLevel = 'full';
  appState.showToast.mockReset();
  uiState.isPublicShare = false;
  apiMocks.delete.mockReset();
  apiMocks.delete.mockResolvedValue({ success: true });
  // The page-wide deleted set is a real module store — reset it, or one test's
  // deletes hide the next test's images.
  useDeletedRefIds.setState({ ids: new Set() });
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

  it('an UNSETTLED tool call renders no body text — "No change" claims a result nothing produced yet', () => {
    // Defect: the chip above a held approval card said "No change" because an
    // unsettled call has no result text; an empty body is the honest state.
    const html = render(createElement(TurnNodes, {
      nodes: [node('tool-call', {
        root: {
          kind: 'tool-call', callId: 'c1', name: 'edit_document',
          argsRaw: '{"document_id":"d1","edits":[]}',
        },
      })],
      isStreaming: true,
    })).textContent ?? '';
    expect(html).toContain('edit_document');
    expect(html).not.toContain('agentStepNoop');
  });

  it('a SETTLED call with no result text keeps the nothing-done note', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('tool-call', {
        root: {
          kind: 'tool-result', callId: 'c1',
          call: { name: 'noop_tool', argsRaw: '{}' }, content: [], isError: false,
        },
      })],
      isStreaming: false,
    }));
    act(() => { html.querySelector('button[aria-expanded]')!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(html.textContent).toContain('agentStepNoop');
  });

  it('a pending verdict ask shows the held call\'s args read off its tool-call node', () => {
    // dsh appends the tool/call node BEFORE asking (the isDecidedVerdict
    // INVARIANT); the ask frame itself carries only {turn, callId, toolName},
    // so the args come from that node.
    const html = render(createElement(TurnNodes, {
      nodes: [
        node('tool-call', {
          root: {
            kind: 'tool-call', callId: 'c9', name: 'edit_document',
            argsRaw: '{"document_id":"d1","edits":[{"old_string":"old line","new_string":"new line"}]}',
          },
        }, 1),
        node('verdict-ask', { callId: 'c9', toolName: 'edit_document' }, 2),
      ],
      isStreaming: true,
    }));
    const text = html.textContent ?? '';
    expect(text).toContain('verdictAllowOnce');
    // The card's own diff pair (the header gist quoting the args is a span, not a pre).
    const pres = [...html.querySelectorAll('pre')];
    expect(pres.length).toBe(2);
    expect(pres[0].textContent).toBe('old line');
    expect(pres[0].className).toContain('text-red');
    expect(pres[1].textContent).toBe('new line');
    expect(pres[1].className).toContain('text-green');
    // The chip above the card no longer claims a result ("No change" is gone).
    expect(text).not.toContain('agentStepNoop');
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

  const image = (status: string, seq = 3): TurnNodeLike => node('image-gen', status === 'done'
    ? { turn: 0, runId: 'r1', status, imageRefIds: ['ref-1'] }
    : { turn: 0, runId: 'r1', status, error: 'queue full' }, seq);

  it.each(['done', 'running', 'failed'])('a closed group holding a %s image run stays open', status => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), g), inGroup(image(status), g)],
      isStreaming: false,
    }));
    expect(html.querySelector('button[aria-expanded]')!.getAttribute('aria-expanded')).toBe('true');
    expect(headers(html)).toContain('a');
  });

  it('a collapsed group opens when an image run joins it after the turn ended', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), g), inGroup(tool(2, 'b'), g)],
      isStreaming: false,
    }));
    expect(headers(html)).toEqual(['processGroupDone_tools']);
    render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), g), inGroup(tool(2, 'b'), g), inGroup(image('running'), g)],
      isStreaming: false,
    }));
    expect(headers(html).slice(0, 3)).toEqual(['processGroupDone_tools', 'a', 'b']);
  });

  it('the group with an image stays open when its streaming turn closes', () => {
    const live = { members: 2, closed: false, summary: { counts: [], running: 'tools', runningDetail: '' } } as unknown as TurnNodeLike['group'];
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), live), inGroup(image('done'), live)],
      isStreaming: true,
    }));
    const g = closed();
    render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), g), inGroup(image('done'), g)],
      isStreaming: false,
    }));
    expect(html.querySelector('button[aria-expanded]')!.getAttribute('aria-expanded')).toBe('true');
  });

  it('the launching generate_image call holds a closed group open before the run card lands', () => {
    const g = closed();
    const html = render(createElement(TurnNodes, {
      nodes: [inGroup(tool(1, 'a'), g), inGroup(tool(2, 'generate_image'), g)],
      isStreaming: false,
    }));
    expect(headers(html)).toEqual(['processGroupDone_tools', 'a', 'generate_image']);
  });

  it('a user fold of an image group sticks', () => {
    const g = closed();
    const nodes = [inGroup(tool(1, 'a'), g), inGroup(image('done'), g)];
    const html = render(createElement(TurnNodes, { nodes, isStreaming: false }));
    press(html.querySelector('button[aria-expanded]')!);
    render(createElement(TurnNodes, { nodes: [...nodes], isStreaming: false }));
    expect(headers(html)).toEqual(['processGroupDone_tools']);
  });
});

describe('TurnNodes — image delete (culling a batch)', () => {
  const imageNode = (refIds: string[], extra: Record<string, unknown> = {}): TurnNodeLike =>
    node('image-gen', { turn: 0, runId: 'r1', status: 'done', imageRefIds: refIds, ...extra });
  const thumbs = (html: HTMLElement) => [...html.querySelectorAll('button[aria-label="viewGeneratedImage"]')];
  const dialog = () => document.body.querySelector('[role="dialog"]');
  const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  const pressKey = (key: string) =>
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key })); });

  it.each([
    ['readonly access', () => { appState.accessLevel = 'readonly'; }],
    ['a public share', () => { uiState.isPublicShare = true; appState.accessLevel = 'full'; }],
  ])('no delete in the lightbox for %s — no trash, the Delete key is inert', (_label, arrange) => {
    arrange();
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    expect(dialog()).not.toBeNull();
    expect(dialog()!.querySelector('button[aria-label="deleteImage"]')).toBeNull();
    pressKey('Delete');
    pressKey('Delete');
    expect(apiMocks.delete).not.toHaveBeenCalled();
  });
});

