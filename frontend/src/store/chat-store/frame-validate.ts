// SYSTEM: chat-frame-validation — runtime shape guard for chat turn frames.
//
// Every frame is dispatched as `(ev: any)` (streaming.ts) with only JSON.parse's
// syntax check between the wire and the handlers. A structurally-wrong frame (an
// `ids` without user_message_id, a numeric `content`) would be processed with
// `undefined` fields and cast unchecked. This hand-rolled discriminated validator
// (no zod — the repo has none, matching the thin-client precedent) rejects
// malformed KNOWN frames; unknown types pass through.
//
// INVARIANT: an unknown `type` — and an unknown dsh `kind` inside a dsh_event —
// ALWAYS passes. Why: the dsh vocabulary relays end to end, so a kind the
// assembler has no definition for still reaches the browser (its node
// definitions — not a relay filter — decide that it produces no node).
// A renderer is an improvement, never
// the condition for being visible. That is what the guards below are NOT for:
// a KNOWN frame missing a
// field its handler destructures is a protocol breach, not a new kind, and
// dropping it is only safe because the caller toasts.

export type Frame = { type: string } & Record<string, unknown>;

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === 'string';

/**
 * Per-type required-field guards. A type absent from this map is a known no-field
 * frame (context_warning/model_update/…) or an unknown forward-compat type — both
 * accepted as long as `type` is a string. Only listed types are strictly checked.
 *
 * A type belongs here when a missing field would write SILENT garbage into the
 * store: an `ids` without user_message_id seats the user bubble and the
 * assistant's parent link as undefined, and the turn still looks fine.
 *
 * The TERMINAL frames (`error`, `lore/halt`) are deliberately NOT here — this
 * is the correction the guards needed rather than the deletion they got. Dropping
 * `error` would lose the turn's failure notice, a worse silence than the one a
 * guard prevents; `lore/halt` renders a card that needs `reason` alone. Both
 * instead degrade HONESTLY — the toast in the `error` arm, the assembler's
 * fallback for a mint it cannot place.
 */
const GUARDS: Record<string, (f: Record<string, unknown>) => boolean> = {
  ids: f => isStr(f.user_message_id) && isStr(f.assistant_message_id),
  sources: f => Array.isArray(f.sources),
  // Per-turn context-occupation signal (used + cap = ints). Emitted by the
  // driver before the turn's `done` content frame; relayed by the backend reducer.
  context_usage: f => typeof f.used === 'number' && typeof f.cap === 'number',
  // The assembler's input. ONLY `kind` is required — it names the node
  // definition, and a kind without one still publishes as the neutral fallback
  // chip. `seq` may be null on a degenerate event (a real v4 log row always
  // carries one), so it may not gate visibility (the
  // feed drops a seq-less frame at its own door, not here).
  dsh_event: f => isStr(f.kind),
};

/** The guarded types, for the test that binds this map to the handler table. */
export const GUARDED_TYPES = Object.keys(GUARDS);

/**
 * Return the frame typed as Frame when it is well-formed for its `type`, else
 * null (caller warns + toasts once per stream, then drops). Never throws.
 */
export function validateFrame(obj: unknown): Frame | null {
  if (!isObj(obj) || !isStr(obj.type)) return null;
  const guard = GUARDS[obj.type];
  if (guard && !guard(obj)) return null;
  return obj as Frame;
}
