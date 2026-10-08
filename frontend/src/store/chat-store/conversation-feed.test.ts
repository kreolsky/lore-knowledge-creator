/** Tests for the browser feed — the assembler driven from Lore's chat store. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
// ?raw: the INVARIANT test pins the SOURCE (the single splice site), same as
// theme.test.ts reads its derived-source mapping.
import feedSource from './conversation-feed.ts?raw';
import {
  isFeedFrame, feedFrame, beginTurn, endTurn, replaceWindowFromRows,
  rewindToLineage, clearFeed, __flushFeedPublishForTest, type FeedPublication,
} from './conversation-feed';

// The relay frames the plugin/backend emit (map.ts toRelayFrame + flat lore mints).
const DSH = (kind: string, seq: number, data: unknown = {}, extra: Record<string, unknown> = {}) =>
  ({ type: 'dsh_event', kind, seq, time: 1000 + seq, data, ...extra });
const LORE = (kind: string, seq: number, data: unknown = {}) =>
  ({ type: kind, seq, time: 1000 + seq, data, ignorable: true });
/** A settled assistant step (the v4 log holds only settled events:
 * `assistant/message` carries the whole
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
    feed(DSH('turn/end', 5, { turn: 0, reason: { kind: 'aborted', reason: { kind: 'user' } } }));
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

describe('dsh process groups on the published nodes', () => {
  const TOOL = (seq: number, callId: string) =>
    DSH('tool/call', seq, { turn: 0, step: 0, callId, name: 'search', arguments: '{}' });
  const RESULT = (seq: number, callId: string) =>
    DSH('tool/result', seq, { turn: 0, step: 0, message: { content: [{ type: 'text', text: 'found', isError: false }], source: { callId } } });

  it('two tool calls share one group; the reply stays outside; the group closes with the turn', () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('turn/start', 2, { turn: 0 }));
    feed(DSH('step/start', 3, { turn: 0, step: 0 }));
    feed(TOOL(4, 'c1'));
    feed(RESULT(5, 'c1'));
    feed(TOOL(6, 'c2'));
    feed(RESULT(7, 'c2'));
    const open = last(drain()).conversation.filter(n => n.kind === 'tool-call');
    expect(open).toHaveLength(2);
    expect(open[0].groupKey).toBeDefined();
    expect(open[1].groupKey).toBe(open[0].groupKey);
    expect(open[0].group).toBe(open[1].group);
    expect(open[0].group?.members).toBe(2);
    expect(open[0].group?.closed).toBe(false);

    feed(DSH('step/end', 8, { turn: 0, step: 0 }));
    feed(DSH('step/start', 9, { turn: 0, step: 1 }));
    feed(ASSISTANT(10, 'Done', { turn: 0, step: 1 }));
    feed(DSH('step/end', 11, { turn: 0, step: 1 }));
    feed(DSH('turn/end', 12, { turn: 0, reason: { kind: 'completed' } }));
    const conv = last(drain()).conversation;
    const tools = conv.filter(n => n.kind === 'tool-call');
    // dsh closed the group: the tool VMs are re-minted to carry it.
    expect(tools[0].group?.closed).toBe(true);
    expect(tools[0]).not.toBe(open[0]);
    const reply = conv.find(n => n.kind === 'assistant-step');
    expect(reply?.groupKey).toBeUndefined();
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
    // The abandoned lineage's nodes are gone — every anchor sits below the
    // abandoned window's min seq (6). 0.2.0's assembler also synthesizes
    // fractional anchors (turn-process at start+0.9, turn-tail at end+0.1),
    // so the bound is the abandoned MIN, not the fork's last fed seq.
    expect(conv.every(n => n.anchorSeq < 6)).toBe(true);
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

describe('the one-mint-site INVARIANT (the browser never mints a lore node)', () => {
  it('feeds a backend-minted lore/image-gen frame through splice like any lore frame', () => {
    // The live card arrives as ONE opaque frame on the chat channel; the
    // browser's only job is to feed it — the assembler places it behind the
    // dispatching tool/call exactly as the reload's frames do.
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('tool/call', 6, { turn: 0, step: 0, callId: 'c1', name: 'generate_image', arguments: '{}' }));
    feed(LORE('lore/image-gen', 6.6, { turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref1'] }));
    const conv = last(drain()).conversation;
    const mint = conv.find(n => n.kind === 'image-gen');
    expect(mint).toBeDefined();
    expect(mint!.anchorSeq).toBe(6.6);
  });

  it('conversation-feed.ts splices EXACTLY once — inside feedFrame (the INVARIANT mechanized)', () => {
    // The load-bearing rule, pinned on the source: every lore/* node the
    // browser shows was minted by the backend and arrives verbatim. A second
    // splice site in this file is a second mint site — the defect this
    // INVARIANT exists to prevent (the browser mint drifted from the
    // backend's three times).
    const source = feedSource;
    expect(source.split('.splice(').length - 1).toBe(1);
    // And the one splice is inside feedFrame — the verbatim feed.
    const fromFeedFrame = source.slice(source.indexOf('export function feedFrame'));
    expect(fromFeedFrame.split('.splice(').length - 1).toBe(1);
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

describe("the attempt's end retires the transient live-chunk rows", () => {
  // The live tail's terminal: the `end` frame retires its attempt's
  // transient rows through the bundle's settleAssistant — an abandoned
  // attempt's ghost text must not survive the attempt, and a committed
  // settlement arrives separately as a normal dsh_event (streaming.dsh-event
  // .test.ts drives the same frames through the store; these drive the feed
  // module directly).
  const START = { type: 'dsh_stream', frame: { type: 'start', attemptId: 'a1', revision: 1, turn: 0, step: 0 } };
  const CHUNK = (index: number, text: string) => ({
    type: 'dsh_stream',
    frame: { type: 'chunk', attemptId: 'a1', revision: 1, index, time: 2000 + index, chunk: { type: 'text-delta', index: 0, text } },
  });
  const END = (outcome: Record<string, unknown>) => ({
    type: 'dsh_stream',
    frame: { type: 'end', attemptId: 'a1', revision: 1, index: 3, outcome },
  });
  const openStep = () => {
    const feed = (f: Record<string, unknown>) => feedFrame(f, 's1', vi.fn());
    feed(DSH('turn/start', 2, { turn: 0 }));
    feed(DSH('step/start', 3, { turn: 0, step: 0 }));
    return feed;
  };
  const streamedText = (pubs: FeedPublication[]): string => {
    const node = last(pubs).conversation.find(n => n.kind === 'assistant-step');
    const blocks = (node?.data as { blocks?: { kind: string; text?: string }[] } | undefined)?.blocks ?? [];
    return blocks.filter(b => b.kind === 'text').map(b => b.text ?? '').join('');
  };

  it('an abandoned end leaves no live-chunk text — no ghost survives the attempt', () => {
    const feed = openStep();
    feed(START);
    feed(CHUNK(0, 'Hel')); feed(CHUNK(1, 'lo')); feed(CHUNK(2, '!'));
    expect(streamedText(drain())).toBe('Hello!');
    feed(END({ kind: 'abandoned' }));
    expect(streamedText(drain())).toBe('');
  });

  it('a committed end retires the transients; the settled message renders alone', () => {
    const feed = openStep();
    feed(START);
    feed(CHUNK(0, 'Hel')); feed(CHUNK(1, 'lo'));
    feed(END({ kind: 'committed', eventType: 'assistant/message', seq: 4 }));
    // The retirement lands at the end itself; the settlement is the NEXT
    // frame and republishes the text as settled state.
    expect(streamedText(drain())).toBe('');
    feed(ASSISTANT(4, 'Hello world'));
    const conv = last(drain()).conversation;
    expect(conv.filter(n => n.kind === 'assistant-step')).toHaveLength(1);
    expect(streamedText(drain())).toBe('Hello world');
  });

  it('a chunk after the end is dropped — the attempt is closed', () => {
    const feed = openStep();
    feed(START);
    feed(CHUNK(0, 'Hel'));
    feed(END({ kind: 'abandoned' }));
    feed(CHUNK(1, 'lo'));
    expect(streamedText(drain())).toBe('');
  });

  it("a lost start's end retires nothing and never clobbers a different open attempt", () => {
    const feed = openStep();
    feed(START);
    feed(CHUNK(0, 'Hel')); feed(CHUNK(1, 'lo'));
    expect(streamedText(drain())).toBe('Hello');
    // An end naming an attempt this feed never saw (the start lost with a
    // socket gap): it retires nothing, and the OPEN attempt must survive it.
    feed({ type: 'dsh_stream', frame: { type: 'end', attemptId: 'other', revision: 1, index: 3, outcome: { kind: 'abandoned' } } });
    expect(streamedText(drain())).toBe('Hello');
    // The open attempt still seats chunks — the foreign end did not close it.
    feed(CHUNK(2, '!'));
    expect(streamedText(drain())).toBe('Hello!');
    // The open attempt closes only when the end names it.
    feed(END({ kind: 'abandoned' }));
    expect(streamedText(drain())).toBe('');
  });
});
