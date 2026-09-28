/**
 * Provider ↔ local-copy contract: the local Y.Doc is loaded BEFORE the socket sync,
 * the sync carries it up, and an auth-class close drops it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import 'fake-indexeddb/auto';
import { IDBFactory } from 'fake-indexeddb';
import * as Y from 'yjs';
import type { DocCollabCallbacks } from '../doc-callbacks';
import { YjsProjectProvider } from '../yjs-provider';
import { MSG_SYNC, SYNC_STEP2, unwrapBinary, wrapBinary } from '../yjs-binary';
import { attachLocalDoc, clearAllLocalDocs } from '../local-doc-persistence';

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];
  static OriginalWebSocket: typeof WebSocket | null = null;

  readyState = 1;
  url = '';
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: ArrayBuffer | string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: (string | ArrayBufferLike)[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string | ArrayBufferLike) { this.sent.push(data); }
  close() { this.readyState = 3; }

  simulateOpen() { this.readyState = 1; this.onopen?.(); }
  simulateBinary(data: Uint8Array) { this.onmessage?.({ data: data.buffer as ArrayBuffer }); }
  simulateClose(code = 1000, reason = '') { this.readyState = 3; this.onclose?.({ code, reason }); }

  static reset() { MockWebSocket.instances = []; }
  static latest() { return MockWebSocket.instances[MockWebSocket.instances.length - 1]; }
  static install() {
    MockWebSocket.OriginalWebSocket = globalThis.WebSocket;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).WebSocket = MockWebSocket;
  }
  static restore() {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (MockWebSocket.OriginalWebSocket) (globalThis as any).WebSocket = MockWebSocket.OriginalWebSocket;
  }
}

function makeCallbacks(): DocCollabCallbacks {
  return {
    onPresenceUsers: vi.fn(),
    onUserJoined: vi.fn(),
    onUserLeft: vi.fn(),
    onDocDeleted: vi.fn(),
    onAccessChanged: vi.fn(),
    onAccessRevoked: vi.fn(),
    onStatusChange: vi.fn(),
    onSynced: vi.fn(),
    onError: vi.fn(),
    onBacklinksChanged: vi.fn(),
    onCheckpointCreated: vi.fn(),
    onDocumentHistoryAdded: vi.fn(),
    onSaveDegraded: vi.fn(),
    onSaveRecovered: vi.fn(),
  };
}

const localUser = { id: 'user1', name: 'User One', color: '#2563eb' };
const PROJECT = 'proj1';
const ENTITY = 'e1';

/** Poll until `predicate` holds — IndexedDB work resolves on real macrotasks. */
async function waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('timed out waiting for condition');
    await new Promise(r => setTimeout(r, 10));
  }
}

/** Poll an async predicate until it holds. */
async function waitForAsync(predicate: () => Promise<boolean>, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!(await predicate())) {
    if (Date.now() > deadline) throw new Error('timed out waiting for condition');
    await new Promise(r => setTimeout(r, 25));
  }
}

/** Text of every SYNC_STEP2 the client pushed for ENTITY, decoded through a fresh doc. */
function pushedStep2Text(ws: MockWebSocket): string {
  const doc = new Y.Doc();
  for (const frame of ws.sent) {
    if (typeof frame === 'string') continue;
    const unwrapped = unwrapBinary(frame as ArrayBuffer);
    if (!unwrapped || unwrapped.entityId !== ENTITY || unwrapped.msgType !== MSG_SYNC) continue;
    if (unwrapped.payload[1] !== SYNC_STEP2) continue;
    Y.applyUpdate(doc, unwrapped.payload.slice(2));
  }
  return doc.getText('content').toString();
}

function joinMessages(ws: MockWebSocket): unknown[] {
  return ws.sent
    .filter((f): f is string => typeof f === 'string')
    .map(f => JSON.parse(f))
    .filter(m => m.type === 'join');
}

async function seedLocalCopy(text: string): Promise<void> {
  const ydoc = new Y.Doc();
  const handle = attachLocalDoc(PROJECT, ENTITY, ydoc)!;
  await handle.whenReady;
  ydoc.getText('content').insert(0, text);
  await handle.flush();
  await handle.detach();
  ydoc.destroy();
}

async function readLocalCopy(): Promise<string> {
  const ydoc = new Y.Doc();
  const handle = attachLocalDoc(PROJECT, ENTITY, ydoc)!;
  await handle.whenReady;
  const text = ydoc.getText('content').toString();
  await handle.detach();
  ydoc.destroy();
  return text;
}

describe('YjsProjectProvider — local persistence', () => {
  beforeEach(async () => {
    MockWebSocket.reset();
    MockWebSocket.install();
    await clearAllLocalDocs();
  });

  afterEach(async () => {
    MockWebSocket.restore();
    await clearAllLocalDocs();
  });

  it('defers the join until the local copy is loaded, and the sync carries it up', async () => {
    await seedLocalCopy('typed during the outage');

    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();

    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    // The join must NOT have gone out yet — a step1 sent now would carry an empty
    // state vector and the local edits would never reach the server.
    expect(joinMessages(ws)).toHaveLength(0);

    await waitFor(() => joinMessages(ws).length === 1);
    expect(entity.ydoc.getText('content').toString()).toBe('typed during the outage');
    expect(pushedStep2Text(ws)).toBe('typed during the outage');

    provider.disconnect();
  });

  it('pushes edits typed while the socket was down on the next sync', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const first = MockWebSocket.latest()!;
    first.simulateOpen();
    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(first).length === 1);

    first.simulateClose(1006, 'network blip');
    entity.ydoc.getText('content').insert(0, 'offline edit');

    await waitFor(() => MockWebSocket.instances.length === 2, 8000);
    const second = MockWebSocket.latest()!;
    second.simulateOpen();

    await waitFor(() => joinMessages(second).length === 1);
    expect(pushedStep2Text(second)).toBe('offline edit');

    provider.disconnect();
  }, 15000);

  it('retires the local copy when the document is left fully synced', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();
    provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(ws).length === 1);

    // The server answers the sync: this client now holds nothing the server does not.
    const serverDoc = new Y.Doc();
    serverDoc.getText('content').insert(0, 'all safely on the server');
    const state = Y.encodeStateAsUpdate(serverDoc);
    const step2 = new Uint8Array(2 + state.length);
    step2[0] = MSG_SYNC;
    step2[1] = SYNC_STEP2;
    step2.set(state, 2);
    ws.simulateBinary(wrapBinary(ENTITY, MSG_SYNC, step2));

    provider.leaveEntity(ENTITY);

    await waitForAsync(async () => (await readLocalCopy()) === '');

    provider.disconnect();
  });

  it('keeps the local copy when the leave happens with edits the server never got', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();
    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(ws).length === 1);
    entity.ydoc.getText('content').insert(0, 'still mine');

    provider.leaveEntity(ENTITY);
    provider.disconnect();

    await waitForAsync(async () => (await readLocalCopy()) === 'still mine');
  });

  it('drops the local copy when the server closes with an access code', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();
    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(ws).length === 1);
    entity.ydoc.getText('content').insert(0, 'revoked content');

    ws.simulateClose(4003, 'no access');

    await waitForAsync(async () => (await readLocalCopy()) === '');

    provider.disconnect();
  });

  it('drops the local copy when the document is deleted', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();
    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(ws).length === 1);
    entity.ydoc.getText('content').insert(0, 'deleted content');

    ws.onmessage?.({ data: JSON.stringify({ type: 'doc_deleted', entity_id: ENTITY }) });

    await waitForAsync(async () => (await readLocalCopy()) === '');

    provider.disconnect();
  });

  it('drops the local copy when access to the entity is revoked', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();
    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(ws).length === 1);
    entity.ydoc.getText('content').insert(0, 'no longer mine');

    ws.onmessage?.({ data: JSON.stringify({ type: 'access_revoked', entity_id: ENTITY }) });

    await waitForAsync(async () => (await readLocalCopy()) === '');

    provider.disconnect();
  });


  it('joins the socket even when the local mirror cannot be opened', async () => {
    // A browser that refuses IndexedDB (quota, private mode, corrupt db) must cost the
    // safety net and nothing else — the join must not wait on a mirror that never loads.
    const refusing = {
      open: () => { throw new Error('idb unavailable'); },
      deleteDatabase: () => ({}),
      databases: async () => [],
    } as unknown as IDBFactory;
    globalThis.indexedDB = refusing;
    try {
      const provider = new YjsProjectProvider(PROJECT, 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();
      provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);

      await waitFor(() => joinMessages(ws).length === 1);

      provider.disconnect();
    } finally {
      globalThis.indexedDB = new IDBFactory();
    }
  });

  it('pushes nothing extra on a reconnect that carries no offline edits', async () => {
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const first = MockWebSocket.latest()!;
    first.simulateOpen();
    provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(first).length === 1);

    // The server hands the document down; the client contributes nothing of its own.
    const serverDoc = new Y.Doc();
    serverDoc.getText('content').insert(0, 'server-side text');
    const state = Y.encodeStateAsUpdate(serverDoc);
    const step2 = new Uint8Array(2 + state.length);
    step2[0] = MSG_SYNC;
    step2[1] = SYNC_STEP2;
    step2.set(state, 2);
    first.simulateBinary(wrapBinary(ENTITY, MSG_SYNC, step2));

    first.simulateClose(1006, 'network blip');
    await waitFor(() => MockWebSocket.instances.length === 2, 8000);
    const second = MockWebSocket.latest()!;
    second.simulateOpen();
    await waitFor(() => joinMessages(second).length === 1);

    // A blind full-state push here would mark the doc dirty server-side, relay the whole
    // document to every peer and enqueue a handoff backup for an edit nobody made.
    expect(pushedStep2Text(second)).toBe('');

    provider.disconnect();
  }, 15000);


  it('typing in the join window is carried by the update stream, not re-pushed later', async () => {
    // Join sent, server step2 not back yet: the entity is joined, so the server DOES
    // receive these updates. Remembering them as unsynced would push the whole document
    // up on the next ordinary sync — the dirty-marking this contract exists to avoid.
    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const first = MockWebSocket.latest()!;
    first.simulateOpen();
    const entity = provider.joinEntity('doc', ENTITY, makeCallbacks(), localUser);
    await waitFor(() => joinMessages(first).length === 1);

    entity.ydoc.getText('content').insert(0, 'typed before step2 came back');
    const serverDoc = new Y.Doc();
    serverDoc.getText('content').insert(0, 'typed before step2 came back');
    const state = Y.encodeStateAsUpdate(serverDoc);
    const step2 = new Uint8Array(2 + state.length);
    step2[0] = MSG_SYNC;
    step2[1] = SYNC_STEP2;
    step2.set(state, 2);
    first.simulateBinary(wrapBinary(ENTITY, MSG_SYNC, step2));

    first.simulateClose(1006, 'network blip');
    await waitFor(() => MockWebSocket.instances.length === 2, 8000);
    const second = MockWebSocket.latest()!;
    second.simulateOpen();
    await waitFor(() => joinMessages(second).length === 1);

    expect(pushedStep2Text(second)).toBe('');

    provider.disconnect();
  }, 15000);

  it('an auth close drops the mirror of a document this session never opened', async () => {
    // The revoked user never joins anything: the app refuses the document before any join.
    // A per-entity sweep would run over an empty map and leave the text on the machine.
    const stray = new Y.Doc();
    const handle = attachLocalDoc(PROJECT, 'never-opened', stray)!;
    await handle.whenReady;
    stray.getText('content').insert(0, 'text from an earlier session');
    await handle.flush();
    await handle.detach();

    const provider = new YjsProjectProvider(PROJECT, 'user1');
    provider.connect();
    const ws = MockWebSocket.latest()!;
    ws.simulateOpen();
    ws.simulateClose(4003, 'no access');

    await waitForAsync(async () => {
      const doc = new Y.Doc();
      const h = attachLocalDoc(PROJECT, 'never-opened', doc)!;
      await h.whenReady;
      const text = doc.getText('content').toString();
      await h.detach();
      doc.destroy();
      return text === '';
    });

    provider.disconnect();
  });

});
