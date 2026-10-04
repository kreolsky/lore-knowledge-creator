/**
 * Collab telemetry producer — turns WS connection lifecycle into telemetry events.
 *
 * // see SYSTEM: telemetry — collab producer (reconnects/closes). The KEY missing data
 * //         point is the close `code`/`reason`/`wasClean`, previously discarded by
 * //         the WS onclose handlers.
 */

import type * as Y from 'yjs';
import { sendTelemetry } from '../telemetry/telemetry';

export type CollabConn = 'yjs' | 'project';

export type CollabEventKind =
  | 'connect'
  | 'open'
  | 'close'
  | 'reconnect-scheduled'
  | 'gave-up'
  /** The client's own liveness deadline fired — a heartbeat went unanswered. */
  | 'ack-deadline'
  /** Per-entity lifecycle on the multiplexed socket — the trace a lost edit is read against. */
  | 'entity-join'
  | 'entity-leave'
  /** The provider was torn down (project page unmounted) — what it held at that moment. */
  | 'disconnect'
  /** An edit landed in a Y.Doc after it was destroyed: accepted on screen, never sent or mirrored. */
  | 'edit-dead-doc'
  /** An edit could not go out because the socket was not open (once per entity per sync). */
  | 'edit-unsent';

export interface CollabEventDetail {
  code?: number;
  reason?: string;
  wasClean?: boolean;
  intentional?: boolean;
  isAuth?: boolean;
  attempt?: number;
  delay?: number;
  /** ack-deadline: the deadline that expired, so the threshold is queryable in telemetry. */
  deadlineMs?: number;
  /** outage duration in ms (open.t − previous close.t) so downtime is queryable */
  outageMs?: number;
  initial?: boolean;
  /** entity-join / entity-leave: consumers still registered on the entity after the call. */
  refCount?: number;
  /** entity-join: a new EntityYjsState was created (vs. another consumer of a live one). */
  fresh?: boolean;
  /** entity-leave: the last consumer left, so the Y.Doc was destroyed. */
  teardown?: boolean;
  /** entity-leave / disconnect: what happened to the IndexedDB mirror. */
  mirror?: 'dropped' | 'kept';
  synced?: boolean;
  unsynced?: boolean | number;
  /** disconnect: how many entities the provider held. */
  entities?: number;
  path?: string;
  /** edit-dead-doc: which teardown destroyed the doc, and how long before the edit. */
  via?: 'leave' | 'disconnect';
  msSinceLeave?: number;
  /** edit-unsent: the socket's readyState when the frame was refused. */
  readyState?: number;
}

/** Build and ship a collab telemetry event. `conn` distinguishes the two WS channels. */
export function logCollabEvent(
  conn: CollabConn,
  kind: CollabEventKind,
  entityId: string | undefined,
  detail: CollabEventDetail = {},
): void {
  sendTelemetry({
    category: 'collab',
    kind,
    entity_id: entityId,
    detail: { conn, ...detail },
  });
}

/**
 * Report the first edit that still lands in a destroyed Y.Doc, then stop listening.
 *
 * WHY: Y.Doc.destroy() drops every observer but keeps accepting transactions, so a CM6
 * view still bound to the doc's Y.Text edits it with nothing sending or mirroring the
 * result — a loss no other signal sees. Attach AFTER destroy(): destroy() clears the
 * observer map, so a listener added before it would be gone.
 */
export function watchDeadDoc(ydoc: Y.Doc, entityId: string, via: 'leave' | 'disconnect'): void {
  const destroyedAt = Date.now();
  const onUpdate = () => {
    ydoc.off('update', onUpdate);
    logCollabEvent('yjs', 'edit-dead-doc', entityId, { via, msSinceLeave: Date.now() - destroyedAt });
  };
  ydoc.on('update', onUpdate);
}
