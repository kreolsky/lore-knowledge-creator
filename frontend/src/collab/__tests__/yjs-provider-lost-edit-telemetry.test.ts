/** Lost-edit telemetry in YjsProjectProvider — the rows that tell a dead binding from an unsent frame. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { DocCollabCallbacks } from '../doc-callbacks';

vi.mock('../../telemetry/telemetry', () => ({ sendTelemetry: vi.fn() }));
vi.mock('../../telemetry/perf', () => ({
  recordRtt: vi.fn(),
  recordAckLatency: vi.fn(),
  recordSyncLatency: vi.fn(),
  recordRecovered: vi.fn(),
}));

import { sendTelemetry } from '../../telemetry/telemetry';
import { YjsProjectProvider } from '../yjs-provider';

const sent = sendTelemetry as unknown as ReturnType<typeof vi.fn>;
const rows = (kind: string) =>
  sent.mock.calls.map((c) => c[0]).filter((ev) => ev.kind === kind);

class MockWebSocket {
  static readonly OPEN = 1;
  static instances: MockWebSocket[] = [];
  static Original: typeof WebSocket | null = null;
  readyState = 1;
  url = '';
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: ArrayBuffer | string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string; wasClean: boolean }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) { this.url = url; MockWebSocket.instances.push(this); }
  send() {}
  close() { this.readyState = 3; }
  static latest() { return MockWebSocket.instances[MockWebSocket.instances.length - 1]; }
}

const callbacks = (): DocCollabCallbacks => ({
  onPresenceUsers: vi.fn(), onUserJoined: vi.fn(), onUserLeft: vi.fn(), onDocDeleted: vi.fn(),
  onAccessChanged: vi.fn(), onAccessRevoked: vi.fn(), onStatusChange: vi.fn(), onSynced: vi.fn(),
  onError: vi.fn(), onBacklinksChanged: vi.fn(), onCheckpointCreated: vi.fn(),
  onDocumentHistoryAdded: vi.fn(), onSaveDegraded: vi.fn(), onSaveRecovered: vi.fn(),
});
const localUser = { id: 'u1', name: 'U', color: '#2563eb' };

function openProvider(): YjsProjectProvider {
  const provider = new YjsProjectProvider('p1', 'u1');
  provider.connect();
  MockWebSocket.latest().onopen?.();
  return provider;
}

describe('YjsProjectProvider lost-edit telemetry', () => {
  beforeEach(() => {
    sent.mockClear();
    MockWebSocket.instances = [];
    MockWebSocket.Original = globalThis.WebSocket;
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = MockWebSocket;
  });
  afterEach(() => {
    if (MockWebSocket.Original) (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket = MockWebSocket.Original;
  });

  it('an edit landing in a doc after its last consumer left is reported once, via leave', () => {
    const provider = openProvider();
    const cb = callbacks();
    const entity = provider.joinEntity('doc', 'e1', cb, localUser);
    provider.leaveEntity('e1', cb);

    entity.ytext.insert(0, 'typed into a dead binding');
    entity.ytext.insert(0, 'more');

    const dead = rows('edit-dead-doc');
    expect(dead).toHaveLength(1);
    expect(dead[0]).toMatchObject({ entity_id: 'e1', detail: expect.objectContaining({ via: 'leave' }) });
    provider.disconnect();
  });

  it('an edit landing in a doc after the provider disconnected is reported, via disconnect', () => {
    const provider = openProvider();
    const entity = provider.joinEntity('doc', 'e1', callbacks(), localUser);
    provider.disconnect();

    entity.ytext.insert(0, 'x');

    expect(rows('edit-dead-doc')).toEqual([
      expect.objectContaining({ entity_id: 'e1', detail: expect.objectContaining({ via: 'disconnect' }) }),
    ]);
  });

  it('a live doc edit with an open socket reports neither dead-doc nor unsent', () => {
    const provider = openProvider();
    const entity = provider.joinEntity('doc', 'e1', callbacks(), localUser);
    entity.ytext.insert(0, 'fine');
    expect(rows('edit-dead-doc')).toHaveLength(0);
    expect(rows('edit-unsent')).toHaveLength(0);
    provider.disconnect();
  });

  it('edits refused by a closed socket report edit-unsent once per episode, with readyState', () => {
    const provider = openProvider();
    const entity = provider.joinEntity('doc', 'e1', callbacks(), localUser);
    MockWebSocket.latest().readyState = 3;

    entity.ytext.insert(0, 'a');
    entity.ytext.insert(0, 'b');

    const unsent = rows('edit-unsent');
    expect(unsent).toHaveLength(1);
    expect(unsent[0]).toMatchObject({ entity_id: 'e1', detail: expect.objectContaining({ readyState: 3 }) });
    provider.disconnect();
  });

  it('join and leave rows carry refCount, freshness and the teardown outcome', () => {
    const provider = openProvider();
    const a = callbacks();
    const b = callbacks();
    provider.joinEntity('doc', 'e1', a, localUser);
    provider.joinEntity('doc', 'e1', b, localUser);
    provider.leaveEntity('e1', a);
    provider.leaveEntity('e1', b);

    expect(rows('entity-join').map((r) => r.detail)).toEqual([
      expect.objectContaining({ refCount: 1, fresh: true }),
      expect.objectContaining({ refCount: 2, fresh: false }),
    ]);
    const leaves = rows('entity-leave').map((r) => r.detail);
    expect(leaves[0]).toMatchObject({ refCount: 1, teardown: false });
    expect(leaves[1]).toMatchObject({ refCount: 0, teardown: true });
    expect(['dropped', 'kept']).toContain(leaves[1].mirror);
    provider.disconnect();
  });

  it('disconnect reports what the provider held and the page path', () => {
    const provider = openProvider();
    provider.joinEntity('doc', 'e1', callbacks(), localUser);
    provider.joinEntity('doc', 'e2', callbacks(), localUser);
    provider.disconnect();

    expect(rows('disconnect')).toEqual([
      expect.objectContaining({ detail: expect.objectContaining({ entities: 2, unsynced: 0, path: window.location.pathname }) }),
    ]);
  });
});
