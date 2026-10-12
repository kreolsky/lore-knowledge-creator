/** The chat queue as the browser shows it — a pure fold of dsh's next-turn
 * inbox: the messages GET's snapshot (`queue` + `queue_seq`) plus the relayed
 * `agent/inbox/spliced` events with a seq above it.
 *
 * The queue itself lives in the session's live dsh agent (see SYSTEM:
 * harness-driver, session-agents.ts); this module only replays its splices,
 * the same splice the inbox projection applies
 * (vendor/dsh/packages/core/agent-loop/src/inbox.ts). Steer (next-step) is not
 * a Lore surface, so its splices are ignored. */

/** One queued message, as a chip renders it. */
export interface QueuedMessage {
  /** dsh's message id — what `/inbox/discard` takes. */
  id: string;
  /** The text blocks of the dsh user message, joined. */
  text: string;
  /** Image blocks riding the message (the user's own plus merged context images). */
  images: number;
}

export interface InboxSplice {
  seq: number;
  start: number;
  removedCount: number;
  inserted: QueuedMessage[];
}

export interface InboxSnapshot {
  items: QueuedMessage[];
  /** The log seq the snapshot includes; null = no live agent (empty inbox). */
  seq: number | null;
}

/** The fold's inputs: no snapshot yet ⇒ nothing is shown (a splice alone
 * cannot be placed — its indices are relative to a list the browser lacks). */
export interface InboxFold {
  snapshot: InboxSnapshot | null;
  splices: InboxSplice[];
}

export const EMPTY_FOLD: InboxFold = { snapshot: null, splices: [] };

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/** A dsh UserMessage → a chip; null when it carries no id. */
export function toQueuedMessage(raw: unknown): QueuedMessage | null {
  if (!isObj(raw) || typeof raw.id !== 'string' || !raw.id) return null;
  const blocks = Array.isArray(raw.content) ? raw.content.filter(isObj) : [];
  const text = blocks
    .filter(b => b.type === 'text' && typeof b.text === 'string')
    .map(b => b.text as string)
    .join('\n');
  return { id: raw.id, text, images: blocks.filter(b => b.type === 'image').length };
}

const toQueued = (list: unknown): QueuedMessage[] =>
  (Array.isArray(list) ? list : []).map(toQueuedMessage).filter((m): m is QueuedMessage => m !== null);

/** The messages GET's `queue` / `queue_seq` pair. */
export function snapshotFrom(queue: unknown, seq: unknown): InboxSnapshot {
  return { items: toQueued(queue), seq: typeof seq === 'number' ? seq : null };
}

/** A relayed `dsh_event` frame → a next-turn splice; null for anything else
 * (another kind, the next-step target, a frame without a numeric seq). */
export function spliceFromFrame(frame: unknown): InboxSplice | null {
  if (!isObj(frame) || frame.type !== 'dsh_event' || frame.kind !== 'agent/inbox/spliced') return null;
  const data = frame.data;
  if (!isObj(data) || data.target !== 'next-turn') return null;
  if (typeof frame.seq !== 'number' || typeof data.start !== 'number') return null;
  return {
    seq: frame.seq,
    start: data.start,
    removedCount: typeof data.removedCount === 'number' ? data.removedCount : 0,
    inserted: toQueued(data.inserted),
  };
}

/** Add one splice; a seq already held (a resync replay) is a no-op. */
export function withSplice(fold: InboxFold, splice: InboxSplice): InboxFold {
  if (fold.splices.some(s => s.seq === splice.seq)) return fold;
  return { ...fold, splices: [...fold.splices, splice] };
}

/** Seat a snapshot; splices it already includes are dropped. A splice that
 * arrived before the snapshot and lies above it is kept — it is the one
 * change the GET could not have seen. */
export function withSnapshot(fold: InboxFold, snapshot: InboxSnapshot): InboxFold {
  const above = (s: InboxSplice) => snapshot.seq === null || s.seq > snapshot.seq;
  return { snapshot, splices: fold.splices.filter(above) };
}

/** The queue to render: the snapshot with every splice above it applied in
 * seq order. */
export function foldQueue(fold: InboxFold): QueuedMessage[] {
  if (!fold.snapshot) return [];
  const base = fold.snapshot.seq;
  return [...fold.splices]
    .filter(s => base === null || s.seq > base)
    .sort((a, b) => a.seq - b.seq)
    .reduce<QueuedMessage[]>((items, s) => {
      const next = [...items];
      next.splice(s.start, s.removedCount, ...s.inserted);
      return next;
    }, fold.snapshot.items);
}
