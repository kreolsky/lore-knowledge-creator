/**
 * Collab telemetry producer — turns WS connection lifecycle into telemetry events.
 *
 * // see SYSTEM: telemetry — collab producer (reconnects/closes). The KEY missing data
 * //         point is the close `code`/`reason`/`wasClean`, previously discarded by
 * //         the WS onclose handlers.
 */

import { sendTelemetry } from '../telemetry/telemetry';

export type CollabConn = 'yjs' | 'project';

export type CollabEventKind =
  | 'connect'
  | 'open'
  | 'close'
  | 'reconnect-scheduled'
  | 'gave-up'
  /** The client's own liveness deadline fired — a heartbeat went unanswered. */
  | 'ack-deadline';

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
