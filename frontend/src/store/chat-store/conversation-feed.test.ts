/** Tests for the browser feed — the assembler driven from Lore's chat store. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  isFeedFrame, feedFrame, beginTurn, endTurn, replaceWindowFromRows, mintImageGen,
  rewindToLineage, clearFeed, __flushFeedPublishForTest, type FeedPublication,
} from './conversation-feed';

// The relay frames the plugin/backend emit (map.ts toRelayFrame + flat lore mints).
const DSH = (kind: string, seq: number, data: unknown = {}, extra: Record<string, unknown> = {}) =>
  ({ type: 'dsh_event', kind, seq, time: 1000 + seq, data, ...extra });
const LORE = (kind: string, seq: number, data: unknown = {}) =>
  ({ type: kind, seq, time: 1000 + seq, data, ignorable: true });
/** A settled assistant step (v3 log: `assistant/message` carries the whole
 * text; deltas never enter the log — the live tail rides `dsh_stream`). */
const ASSISTANT = (seq: number, text: string, at: { turn: number; step: number } = { turn: 0, step: 0 }) =>
  DSH('assistant/message', seq, {
    ...at, message: { id: `m${seq}`, role: 'assistant', content: [{ type: 'text', text }], source: { kind: 'model', provider: 'lore', model: 'test' } }, stream: [],
  }, { surfaceOp: 'append' });

function drain(): FeedPublication[] {
  const pubs: FeedPublication[] = [];
  const set = (p: FeedPublication) => pubs.push(p);
  __flushFeedPublishForTest(set);
  return pubs;
}

// A publication patch carries each field independently; the tests read through
// the defaults so `pub.conversation` is always an array.
const last = (pubs: FeedPublication[]): Required<FeedPublication> => ({
  conversation: [], turnRanges: {}, turnStartSeq: null, ...pubs[pubs.length - 1],
});

beforeEach(() => {
  clearFeed(vi.fn());
});

describe('isFeedFrame', () => {
  it('admits dsh_event and lore/* only', () => {
    expect(isFeedFrame({ type: 'dsh_event' })).toBe(true);
    expect(isFeedFrame({ type: 'lore/halt' })).toBe(true);
    expect(isFeedFrame({ type: 'ids' })).toBe(false);
    expect(isFeedFrame({ type: 'session_title' })).toBe(false);
    expect(isFeedFrame({ type: 'some_future_type' })).toBe(false);
    expect(isFeedFrame({})).toBe(false);
  });
});

describe('feedFrame → the assembler', () => {
  it('assembles a turn: user message, assistant text, tool call, result', () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('user/message', 1, { id: 'u1', content: [{ type: 'text', text: 'hi' }], source: { kind: 'user' } }, { surfaceOp: 'append' }));
    feed(DSH('turn/start', 2, { turn: 0 }));
    feed(DSH('step/start', 3, { turn: 0, step: 0 }));
    feed(ASSISTANT(5, 'Hello'));
    feed(DSH('tool/call', 6, { turn: 0, step: 0, callId: 'c1', name: 'search', arguments: '{}' }));
    feed(DSH('tool/result', 7, { turn: 0, step: 0, message: { content: [{ type: 'text', text: 'found', isError: false }], source: { callId: 'c1' } } }));
    feed(DSH('turn/end', 8, { turn: 0, reason: { kind: 'completed' } }));

    const kinds = drain().flatMap(p => (p.conversation ?? []).map(n => n.kind));
    expect(kinds).toContain('user');
    expect(kinds).toContain('assistant-step');
    expect(kinds).toContain('tool-call');
  });

  it('routes a behind-the-tail lore mint through splice (the cursor INVARIANT)', () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('turn/start', 2, { turn: 0 }));
    feed(ASSISTANT(4, 'Hi'));
    // A user message fed AFTER the mint must stay session-level / its own turn —
    // a behind-the-tail append would rewind the location cursor onto it.
    feed(LORE('lore/halt', 3.7, { turn: 0, reason: 'aborted' }));
    feed(DSH('turn/end', 5, { turn: 0, reason: { kind: 'aborted' } }));
    const pubs = drain();
    const conv = last(pubs).conversation;
    // The halt node exists...
    expect(conv.some(n => n.kind === 'halt')).toBe(true);
    // ...and a later user message still assembles as a user node (not relocated).
    feed(DSH('user/message', 9, { id: 'u2', content: [{ type: 'text', text: 'next' }], source: { kind: 'user' } }, { surfaceOp: 'append' }));
    const after = last(drain()).conversation;
    expect(after.some(n => n.kind === 'user')).toBe(true);
  });

  it('drops a feed frame without a finite seq (the degenerate relay frame)', () => {
    feedFrame({ type: 'dsh_event', kind: 'turn/start', seq: null }, 's1', vi.fn());
    expect(last(drain()).conversation).toEqual([]);
  });

  it('ignores non-feed frames entirely', () => {
    feedFrame({ type: 'done', content: '' }, 's1', vi.fn());
    expect(last(drain()).conversation).toEqual([]);
  });
});

describe('live turn placement', () => {
  it('beginTurn/endTurn bind the turn window to the assistant row', () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    const set = vi.fn();
    beginTurn('s1', set);
    feed(DSH('turn/start', 2, { turn: 0 }));
    feed(ASSISTANT(3, 'Hi'));
    feed(DSH('turn/end', 4, { turn: 0, reason: { kind: 'completed' } }));
    endTurn('s1', 'a1', set);
    const ranges = set.mock.calls.map(c => c[0]).filter(p => p.turnRanges)[0].turnRanges;
    expect(ranges.a1).toEqual({ min: 2, max: 4 });
  });

  it('a reload window rebuilds the ranges and publishes the whole conversation', () => {
    const rows = [
      { message_id: 'a1', frames: [
        DSH('turn/start', 2, { turn: 0 }),
        ASSISTANT(3, 'Hi'),
        DSH('turn/end', 4, { turn: 0, reason: { kind: 'completed' } }),
        LORE('lore/halt', 4.7, { turn: 0, reason: 'aborted' }),
      ] },
      { message_id: 'a2', frames: [LORE('lore/halt', 9.7, { turn: null, reason: 'disconnected' })] },
      { message_id: 'u1' },
    ];
    const set = vi.fn();
    replaceWindowFromRows(rows as Array<Record<string, unknown>>, 's1', set);
    const pub: FeedPublication = set.mock.calls[0][0];
    expect(pub.turnRanges?.a1).toEqual({ min: 2, max: 4.7 });
    expect(pub.turnRanges?.a2).toEqual({ min: 9.7, max: 9.7 });
    const conv = pub.conversation ?? [];
    expect(conv.some(n => n.kind === 'halt')).toBe(true);
    // Two halt facts at DISTINCT anchors survive (replaceWindow overwrites only
    // same-seq duplicates): the turn's mint and the turn-less row's tail mint.
    expect(conv.filter(n => n.kind === 'halt').length).toBe(2);
  });

  it('a session switch resets the machine (no stale node survives)', () => {
    feedFrame(DSH('turn/start', 1, { turn: 0 }), 's1', vi.fn());
    replaceWindowFromRows([{ message_id: 'a1', frames: [
      DSH('step/start', 5, { turn: 0, step: 0 }),
      ASSISTANT(6, 'Hi'),
    ] }], 's2', vi.fn());
    const pub = last(drain());
    expect(pub.conversation.map(n => n.kind)).toEqual(['turn-process', 'assistant-step']);
  });
});

describe('rewindToLineage — the fork send rewinds the assembler to the lineage it extends', () => {
  // A window of two completed turns on branch A, as a reload loads it.
  const rowsA1A2 = [
    { message_id: 'a1', frames: [
      DSH('turn/start', 2, { turn: 0 }),
      ASSISTANT(3, 'One'),
      DSH('turn/end', 4, { turn: 0, reason: { kind: 'completed' } }),
    ] },
    { message_id: 'a2', frames: [
      DSH('turn/start', 6, { turn: 1 }),
      ASSISTANT(7, 'Two'),
      DSH('turn/end', 8, { turn: 1, reason: { kind: 'completed' } }),
    ] },
  ];

  it('(a) rewinding to NO lineage lets the root fork\'s restarted seqs publish into the streaming turn', () => {
    replaceWindowFromRows(rowsA1A2 as Array<Record<string, unknown>>, 's1', vi.fn());
    // The root fork: no ancestor survives — the new dsh session restarts at 0.
    const set = vi.fn();
    rewindToLineage([], 's1', set);
    // The rewind itself publishes the emptied window...
    expect(set.mock.calls.length).toBe(1);
    expect(set.mock.calls[0][0].turnRanges).toEqual({});
    expect(set.mock.calls[0][0].conversation).toEqual([]);
    // ...and the boundary seats at the rewound tail (nothing held), so every
    // fork node renders in the streaming row.
    const seat = vi.fn();
    beginTurn('s1', seat);
    expect(seat.mock.calls[0][0].turnStartSeq).toBe(Number.NEGATIVE_INFINITY);
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('turn/start', 0, { turn: 0 }));
    feed(ASSISTANT(1, 'Forked'));
    // Seq 2 collides with the abandoned window's turn/start — the exact seq
    // `append` drops as already-held when the rewind is missing.
    feed(DSH('tool/call', 2, { turn: 0, step: 0, callId: 'c1', name: 'search', arguments: '{}' }));
    feed(DSH('turn/end', 3, { turn: 0, reason: { kind: 'completed' } }));
    const conv = last(drain()).conversation;
    expect(conv.some(n => n.kind === 'tool-call')).toBe(true);
    // The abandoned lineage's nodes are gone (nothing above the fork's tail).
    expect(conv.every(n => n.anchorSeq <= 3)).toBe(true);
  });

  it('(b) rewinding to the chain through A1 keeps A1\'s nodes, drops A2\'s, and clears the seq space the continuation reuses', () => {
    replaceWindowFromRows(rowsA1A2 as Array<Record<string, unknown>>, 's1', vi.fn());
    const set = vi.fn();
    rewindToLineage(['a1'], 's1', set);
    const pub: FeedPublication = set.mock.calls[0][0];
    expect(pub.turnRanges).toEqual({ a1: { min: 2, max: 4 } });
    const conv = pub.conversation ?? [];
    expect(conv.some(n => n.kind === 'assistant-step' && n.anchorSeq === 3)).toBe(true);
    expect(conv.some(n => n.anchorSeq === 7)).toBe(false);
    // The branch continuation: the seeded prefix ends at the branch point, so
    // the new turn's frames reuse A2's seq space (6 was A2's turn/start) —
    // with A2 dropped they append cleanly instead of being dropped as held.
    const seat = vi.fn();
    beginTurn('s1', seat);
    expect(seat.mock.calls[0][0].turnStartSeq).toBe(4);
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('turn/start', 5, { turn: 2 }));
    feed(DSH('tool/call', 6, { turn: 2, step: 0, callId: 'c1', name: 'search', arguments: '{}' }));
    expect(last(drain()).conversation.some(n => n.kind === 'tool-call')).toBe(true);
  });

  it('(c) a linear chain is a no-op — no re-window, no publication', () => {
    replaceWindowFromRows(rowsA1A2 as Array<Record<string, unknown>>, 's1', vi.fn());
    const set = vi.fn();
    rewindToLineage(['a1', 'a2'], 's1', set);
    expect(set.mock.calls.length).toBe(0);
    // The window is untouched: A2's nodes still publish.
    expect(last(drain()).conversation.some(n => n.anchorSeq === 7)).toBe(true);
  });
});

describe('the live lore/image-gen mint', () => {
  const callFrame = DSH('tool/call', 6, { turn: 0, step: 0, callId: 'c1', name: 'generate_image', arguments: '{}' });
  // The REAL Tool-API result frame (the shape backend `_tool_result_text`
  // reads — pinned by tests/backend/test_driver_client.py `_applied_edit_result`):
  // the result text is NESTED at message.content[0].content[*].text, wrapped in
  // the Tool-API JSON envelope carrying the run id. The flat
  // `content[0].text` fixture this test used before is a shape the wire never
  // carried — the live anchor join matched only it and silently missed every
  // real run (defect D, user smoke 2026-09-07).
  const resultFrame = DSH('tool/result', 7, {
    turn: 0, step: 0,
    message: {
      source: { kind: 'tool', callId: 'c1' },
      content: [{
        type: 'tool-result', toolCallId: 'c1', isError: false,
        content: [{ type: 'text', text: JSON.stringify({ run_id: 'r1', ok: true }) }],
      }],
    },
  });
  const steps = [
    { tool: 'generate_image', run_id: 'r1', outcome: 'ok', image_ref_ids: ['ref1', 'ref2'], title: 'Doc title' },
    { tool: 'refine_prompt', outcome: 'ok', detail: 'a better prompt' },
  ];

  it('mints at the dispatching call + 0.6 with the payload derived from the step dicts', () => {
    feedFrame(callFrame, 's1', vi.fn());
    feedFrame(resultFrame, 's1', vi.fn());
    const minted = mintImageGen('r1', steps, 's1', vi.fn());
    expect(minted).toBe(true);
    const conv = last(drain()).conversation;
    const mint = conv.find(n => n.kind === 'image-gen');
    expect(mint).toBeDefined();
    expect(mint!.anchorSeq).toBe(6.6);
    const data = mint!.data as Record<string, unknown>;
    expect(data.status).toBe('done');
    expect(data.imageRefIds).toEqual(['ref1', 'ref2']);
    expect(data.refine).toEqual({ ok: true, prompt: 'a better prompt' });
    expect(data.title).toBe('Doc title');
  });

  it('joins the call id through the nested toolCallId when source carries none', () => {
    // The server twin (_result_call_id) falls back to content[0].toolCallId;
    // the browser join must read the same two places.
    feedFrame(callFrame, 's1', vi.fn());
    feedFrame(DSH('tool/result', 7, {
      turn: 0, step: 0,
      message: {
        content: [{
          type: 'tool-result', toolCallId: 'c1', isError: false,
          content: [{ type: 'text', text: JSON.stringify({ run_id: 'r1' }) }],
        }],
      },
    }), 's1', vi.fn());
    expect(mintImageGen('r1', steps, 's1', vi.fn())).toBe(true);
    expect(last(drain()).conversation.some(n => n.kind === 'image-gen')).toBe(true);
  });

  it('derives a failed run the same way the backend does', () => {
    feedFrame(callFrame, 's1', vi.fn());
    feedFrame(resultFrame, 's1', vi.fn());
    mintImageGen('r1', [
      { tool: 'generate_image', run_id: 'r1', outcome: 'failed', detail: 'queue full' },
    ], 's1', vi.fn());
    const data = last(drain()).conversation.find(n => n.kind === 'image-gen')!.data as Record<string, unknown>;
    expect(data.status).toBe('failed');
    expect(data.error).toBe('queue full');
    expect(data.imageRefIds).toBeUndefined();
  });

  it('returns false (the LOUD miss) without a dispatching call or a gen chip', () => {
    // No frames fed at all — no anchor exists.
    expect(mintImageGen('r1', steps, 's1', vi.fn())).toBe(false);
    // The dispatching call fed, but no tool/result carries the run id.
    feedFrame(callFrame, 's1', vi.fn());
    expect(mintImageGen('r1', steps, 's1', vi.fn())).toBe(false);
    // The fed call frame assembles its own tool-call node; the MINT never
    // appears — the caller must surface the miss (no-silent-degradation).
    expect(last(drain()).conversation.some(n => n.kind === 'image-gen')).toBe(false);
  });
});

describe('published node identity — what the renderer\'s slice reuse rests on', () => {
  it('re-publishes a GROWING assistant step as a NEW object while its key holds', () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('turn/start', 1, { turn: 0 }));
    feed(DSH('step/start', 2, { turn: 0, step: 0 }));
    feed(ASSISTANT(4, 'Hel'));
    const first = last(drain()).conversation.find(n => n.kind === 'assistant-step');
    expect(first).toBeDefined();

    feed(ASSISTANT(5, 'Hello'));
    const second = last(drain()).conversation.find(n => n.kind === 'assistant-step');
    expect(second).toBeDefined();

    // The key is STABLE across the delta — which is exactly why a length+key
    // comparison cannot stand in for identity (it would freeze the text).
    expect(second!.key).toBe(first!.key);
    expect(second).not.toBe(first);
  });

  it('re-publishes an UNTOUCHED node as the SAME object', () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('user/message', 1, { id: 'u1', content: [{ type: 'text', text: 'hi' }], source: { kind: 'user' } }, { surfaceOp: 'append' }));
    feed(DSH('turn/start', 2, { turn: 0 }));
    feed(DSH('step/start', 3, { turn: 0, step: 0 }));
    feed(ASSISTANT(5, 'Hel'));
    const firstUser = last(drain()).conversation.find(n => n.kind === 'user');
    expect(firstUser).toBeDefined();

    feed(ASSISTANT(6, 'Hello'));
    const secondUser = last(drain()).conversation.find(n => n.kind === 'user');
    // The user row did not change: the assembler rebuilt only the dirtied
    // context, so the memoized VM is the same object and the renderer reuses
    // that row's slice instead of re-rendering it per token.
    expect(secondUser).toBe(firstUser);
  });
});
