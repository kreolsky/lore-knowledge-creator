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

  it('names the full image after the document title, numbered, so a download is not "full.png"', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('image-gen', {
        turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref-1', 'ref-2'], title: 'Город: Арвен/2',
      })],
      isStreaming: false,
    }));
    const thumbs = html.querySelectorAll<HTMLButtonElement>('button[aria-label="viewGeneratedImage"]');
    act(() => { thumbs[1].click(); });
    const link = document.body.querySelector<HTMLAnchorElement>('[role="dialog"] a[aria-label="download"]')!;
    expect(link.getAttribute('href')).toBe(`/api/files/ref-2/${encodeURIComponent('Город_ Арвен_2-2.png')}`);
    expect(link.getAttribute('download')).toBe('Город_ Арвен_2-2.png');
  });

  it('a generated image with no title downloads as image-{n}.png', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [node('image-gen', { turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref-1'] })],
      isStreaming: false,
    }));
    act(() => { html.querySelector<HTMLButtonElement>('button[aria-label="viewGeneratedImage"]')!.click(); });
    const link = document.body.querySelector<HTMLAnchorElement>('[role="dialog"] a[aria-label="download"]')!;
    expect(link.getAttribute('download')).toBe('image-1.png');
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
  const trashes = (html: HTMLElement) => [...html.querySelectorAll('button[aria-label="deleteImage"]')];
  const dialog = () => document.body.querySelector('[role="dialog"]');
  const dialogDownload = () => dialog()!.querySelector<HTMLAnchorElement>('a[aria-label="download"]')!;
  const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  const pressKey = (key: string) =>
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key })); });
  const flush = () => act(async () => {});

  it('thumbs carry no trash; the lightbox trash arms on the first click and deletes on the second (optimistic)', async () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2', 'ref-3'])], isStreaming: false,
    }));
    expect(thumbs(html).length).toBe(3);
    expect(trashes(html).length).toBe(0); // delete lives only in the lightbox
    click(thumbs(html)[1]);
    const trash = () => dialog()!.querySelector('button[aria-label="deleteImage"]')!;
    click(trash());
    expect(thumbs(html).length).toBe(3); // armed, nothing deleted yet
    expect(apiMocks.delete).not.toHaveBeenCalled();
    click(trash());
    expect(apiMocks.delete).toHaveBeenCalledWith('/references/ref-2');
    expect(thumbs(html).length).toBe(2);
    await flush();
    expect(thumbs(html).length).toBe(2); // resolved delete keeps it gone
  });

  it('a lightbox delete stays open on the next image (clamped), then emptying the run closes it', async () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2', 'ref-3'])], isStreaming: false,
    }));
    click(thumbs(html)[1]); // open at ref-2
    expect(dialogDownload().getAttribute('href')).toContain('/api/files/ref-2/');
    const trash = () => dialog()!.querySelector('button[aria-label="deleteImage"]')!;
    click(trash());
    click(trash());
    expect(apiMocks.delete).toHaveBeenCalledWith('/references/ref-2');
    // Same index, clamped to the shrunk list → the NEXT image (ref-3) stands.
    expect(dialog()).not.toBeNull();
    expect(dialogDownload().getAttribute('href')).toContain('/api/files/ref-3/');
    expect(thumbs(html).length).toBe(2);
    // Delete the rest from the lightbox: ref-3, then ref-1 — the dialog closes
    // when nothing is left and the plate takes the note.
    click(trash());
    click(trash());
    click(trash());
    click(trash());
    await flush();
    expect(dialog()).toBeNull();
    expect(html.textContent).toContain('imagesDeletedByUser');
  });

  it('the Delete key deletes the shown image on the second press (Backspace is the macOS spelling)', async () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    pressKey('Delete');
    expect(apiMocks.delete).not.toHaveBeenCalled();
    pressKey('Delete');
    expect(apiMocks.delete).toHaveBeenCalledWith('/references/ref-1');
    // Still open on the remaining image.
    expect(dialogDownload().getAttribute('href')).toContain('/api/files/ref-2/');
    // Backspace arms + deletes the same way.
    pressKey('Backspace');
    pressKey('Backspace');
    await flush();
    expect(apiMocks.delete).toHaveBeenCalledWith('/references/ref-2');
    expect(dialog()).toBeNull();
    expect(html.textContent).toContain('imagesDeletedByUser');
  });

  it('a failed delete brings the thumb back and states the error (no silent degradation)', async () => {
    apiMocks.delete.mockRejectedValue(new Error('boom'));
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    pressKey('Delete');
    pressKey('Delete');
    expect(thumbs(html).length).toBe(1); // optimistically gone
    await flush();
    expect(thumbs(html).length).toBe(2); // restored
    expect(appState.showToast).toHaveBeenCalledWith('imageDeleteFailed', 'error');
  });

  it('a delete survives a tab switch: the remounted plate still hides it', async () => {
    const nodes = [imageNode(['ref-1', 'ref-2', 'ref-3'])];
    const html = render(createElement(TurnNodes, { nodes, isStreaming: false }));
    click(thumbs(html)[0]);
    pressKey('Delete');
    pressKey('Delete');
    pressKey('Delete');
    pressKey('Delete'); // ref-1 and ref-2 gone
    await flush();
    act(() => { root.render(null); }); // leaving the chat tab unmounts the plate
    act(() => { root.render(createElement(TurnNodes, { nodes, isStreaming: false })); });
    expect(thumbs(container).length).toBe(1);
    expect(container.querySelector('img')!.getAttribute('src')).toContain('ref-3');
  });

  it('a delete from another surface (the page-wide deleted set) hides the thumb', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2'])], isStreaming: false,
    }));
    act(() => { useDeletedRefIds.getState().add(['ref-1']); });
    expect(thumbs(html).length).toBe(1);
    // An id this plate does not show changes nothing.
    act(() => { useDeletedRefIds.getState().add(['ref-9']); });
    expect(thumbs(html).length).toBe(1);
  });

  it('the server-side deletedByUser flag renders the note without any local delete', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode([], { deletedByUser: true })], isStreaming: false,
    }));
    expect(html.textContent).toContain('imagesDeletedByUser');
    expect(thumbs(html).length).toBe(0);
    expect(trashes(html).length).toBe(0);
  });

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

  it('a half-armed lightbox delete disarms on navigation (one press on the next image only arms)', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    pressKey('Delete'); // armed on ref-1
    pressKey('ArrowRight'); // navigate to ref-2 — the arm must not travel
    pressKey('Delete');
    expect(apiMocks.delete).not.toHaveBeenCalled();
    pressKey('Delete'); // armed on ref-2 — now it fires
    expect(apiMocks.delete).toHaveBeenCalledWith('/references/ref-2');
  });

  it('a failed delete of the last image brings the thumb back but never reopens the lightbox', async () => {
    apiMocks.delete.mockRejectedValue(new Error('boom'));
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    pressKey('Delete');
    pressKey('Delete');
    expect(dialog()).toBeNull();
    await flush();
    expect(thumbs(html).length).toBe(1); // restored
    expect(dialog()).toBeNull(); // nobody reopened it
  });

  it('a delete never renames the survivors: the download number is the place in the batch', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2', 'ref-3'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    pressKey('Delete');
    pressKey('Delete'); // ref-1 gone — ref-2 now stands first
    expect(dialogDownload().getAttribute('href')).toMatch(/\/api\/files\/ref-2\/image-2\.png$/);
  });

  it('an errored lightbox image still offers the trash, not the download', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode(['ref-1', 'ref-2'])], isStreaming: false,
    }));
    click(thumbs(html)[0]);
    act(() => { dialog()!.querySelector('img')!.dispatchEvent(new Event('error', { bubbles: true })); });
    expect(dialog()!.textContent).toContain('failedToLoadImages');
    expect(dialog()!.querySelector('a[aria-label="download"]')).toBeNull();
    const trash = () => dialog()!.querySelector('button[aria-label="deleteImage"]')!;
    click(trash());
    click(trash());
    expect(apiMocks.delete).toHaveBeenCalledWith('/references/ref-1');
  });

  it('a run that produced nothing keeps the nothing-done text (not the deleted note)', () => {
    const html = render(createElement(TurnNodes, {
      nodes: [imageNode([])], isStreaming: false,
    })).textContent ?? '';
    expect(html).toContain('agentStepNoop');
    expect(html).not.toContain('imagesDeletedByUser');
  });
});

