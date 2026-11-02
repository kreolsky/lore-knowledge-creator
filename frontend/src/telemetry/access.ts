/** Human document/reference opens → durable per-object access counts (server: object_access_daily). */

import type { AccessVia } from '../events/event-types';
import { sendTelemetry } from './telemetry';

// References use their reference id: they live in `documents`, the server maps it to doc:<id>.
export function reportOpen(entityId: string, via: AccessVia): void {
  sendTelemetry({ category: 'access', kind: 'open', entity_id: entityId, detail: { via } });
}
