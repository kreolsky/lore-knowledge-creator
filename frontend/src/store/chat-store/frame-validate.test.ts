import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { validateFrame, GUARDED_TYPES } from './frame-validate';

/**
 * The handler keys, read out of streaming.ts itself. Derived rather than
 * hand-listed: the map lives inside createTurnSink's closure, and exporting it
 * just to be asserted would widen the interface for the test's benefit.
 *
 * `dsh_event` has no arm in the map — it is HANDLED by the feed dispatch
 * (`isFeedFrame(event)` → conversation-feed), so the dispatch line is part of what "handles" a type.
 */
function streamingHandlerTypes(): string[] {
  const src = readFileSync(
    resolve(process.cwd(), 'src/store/chat-store/streaming.ts'), 'utf8',
  );
  const start = src.indexOf('const handlers: Record<string, (ev: any) => void> = {');
  if (start < 0) throw new Error('handler map not found — did streaming.ts move it?');
  const body = src.slice(start, src.indexOf('\n  };', start));
  const keys = [...body.matchAll(/^    ([a-z_][a-z0-9_]*)\s*\(/gm)].map(m => m[1]);
  if (src.includes('isFeedFrame(event)')) keys.push('dsh_event');
  return keys;
}

// The per-type GUARDS reject a malformed KNOWN frame (the caller warns + toasts
// once per stream, then drops). They never gate an unknown `type` or an unknown
// dsh `kind` — that is what makes them compatible with relaying the dsh
// vocabulary end to end, and conflating the two is what deleted them once.
//
// Two frames are deliberately UNGUARDED: `error` and `lore/halt` are
// TERMINAL, so dropping one would leave the stream with no end state — a worse
// silence than the guard prevents. They render a degraded-but-HONEST notice
// instead (never `⚠ undefined`).

describe('validateFrame', () => {
  it('accepts well-formed frames per type', () => {
    expect(validateFrame({ type: 'ids', user_message_id: 'u', assistant_message_id: 'a' }))
      .toEqual({ type: 'ids', user_message_id: 'u', assistant_message_id: 'a' });
    expect(validateFrame({ type: 'delta', content: 'hi' })).not.toBeNull();
    expect(validateFrame({ type: 'error', message: 'boom' })).not.toBeNull();
    expect(validateFrame({ type: 'sources', sources: [] })).not.toBeNull();
    expect(validateFrame({ type: 'agent_step', tool: 'read_document' })).not.toBeNull();
    expect(validateFrame({ type: 'turn_halted', reason: 'tool_call_limit', message: 'stopped' })).not.toBeNull();
    expect(validateFrame({ type: 'awaiting_verdict', call_id: 'c1', tool_name: 'edit_document', message_id: 'am' })).not.toBeNull();
    expect(validateFrame({ type: 'context_usage', used: 12345, cap: 262144 })).not.toBeNull();
  });

  it('accepts known no-field frames', () => {
    expect(validateFrame({ type: 'context_warning' })).not.toBeNull();
    expect(validateFrame({ type: 'model_update', model: 'm' })).not.toBeNull();
  });

  it('passes unknown types through (forward-compat)', () => {
    expect(validateFrame({ type: 'some_future_frame', x: 1 })).not.toBeNull();
  });

  it('rejects a KNOWN frame missing a field its handler writes to the store', () => {
    // Each of these would otherwise be dispatched and write `undefined` into the
    // store with nothing on screen to say so — an `ids` without user_message_id
    // seats the user bubble and the assistant's parent link as undefined and the
    // turn still looks fine. Rejected here ⇒ the caller toasts once.
    expect(validateFrame({ type: 'ids', assistant_message_id: 'a' })).toBeNull();
    expect(validateFrame({ type: 'sources', sources: 'nope' })).toBeNull();
    expect(validateFrame({ type: 'context_usage', used: 5 })).toBeNull();
  });

  it('passes the TERMINAL frames through even when malformed', () => {
    // Dropping these would lose the turn's failure notice / halt card with no
    // end state at all — a worse silence than the guard prevents. streaming.ts
    // renders the fallback notice instead of `⚠ undefined`.
    expect(validateFrame({ type: 'error' })).not.toBeNull();
    // The halt rides as a lore mint — unguarded for the same reason; the
    // assembler's card is the rendering, and the feed needs the frame.
    expect(validateFrame({ type: 'lore/halt', seq: 5, data: { reason: 'step_limit' } }))
      .not.toBeNull();
    expect(validateFrame({ type: 'lore/halt' })).not.toBeNull();
  });

  it('passes the dsh_event frame through — ANY kind, rendered or not', () => {
    // The guards must not become a visibility gate on the dsh vocabulary: a
    // kind with no node definition still has to reach the assembler, which
    // publishes it as the neutral fallback chip.
    expect(validateFrame({ type: 'dsh_event', kind: 'guard/reminder', seq: 12, data: '{}' }))
      .not.toBeNull();
    expect(validateFrame({ type: 'dsh_event', kind: 'guard/note', seq: null, data: 'null' }))
      .not.toBeNull();
    expect(validateFrame({ type: 'dsh_event', kind: 'totally/unheard-of' })).not.toBeNull();
    // Only the kind that names the node definition is required.
    expect(validateFrame({ type: 'dsh_event', seq: 3 })).toBeNull();
  });

  it('guards only types the dispatcher actually handles', () => {
    // Derived, not hand-copied: a guard for a type no handler reads is dead
    // weight that silently drops frames nothing was going to mishandle, and a
    // renamed frame would leave one behind.
    const handled = new Set(streamingHandlerTypes());
    expect(handled.size).toBeGreaterThan(3);  // the extraction actually found them
    const orphans = GUARDED_TYPES.filter(tp => !handled.has(tp));
    expect(orphans).toEqual([]);
  });

  it('rejects non-frames', () => {
    expect(validateFrame(null)).toBeNull();
    expect(validateFrame(42)).toBeNull();
    expect(validateFrame([])).toBeNull();
    expect(validateFrame({ nothing: true })).toBeNull(); // no type
    expect(validateFrame({ type: 5 })).toBeNull(); // non-string type
  });
});
