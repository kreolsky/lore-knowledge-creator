/** Unit tests for ProjectConnection — mock WebSocket, test lifecycle + dispatch. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ProjectConnection, type ProjectWsCallbacks } from './project-connection';
import { MockWebSocket } from './__tests__/mock-ws';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeCallbacks(overrides: Partial<ProjectWsCallbacks> = {}): ProjectWsCallbacks {
  return {
    onDocumentCreated: vi.fn(),
    onDocumentRenamed: vi.fn(),
    onDocumentMoved: vi.fn(),
    onDocumentReordered: vi.fn(),
    onDocumentDeleted: vi.fn(),
    onDocumentsDeletedBatch: vi.fn(),
    onDocumentsMovedOut: vi.fn(),
    onDocumentsMovedIn: vi.fn(),
    onReferenceCreated: vi.fn(),
    onReferenceRenamed: vi.fn(),
    onReferenceMoved: vi.fn(),
    onReferenceDeleted: vi.fn(),
    onReferenceUpdated: vi.fn(),
    onReferenceStatusChanged: vi.fn(),
    onContentFlushed: vi.fn(),
    onAgentExtractionStarted: vi.fn(),
    onGenerateImageProgress: vi.fn(),
    onGenerateImageDone: vi.fn(),
    onGenerateImageFailed: vi.fn(),
    onExtractionError: vi.fn(),
    onAgentErrorNote: vi.fn(),
    onChatFrame: vi.fn(),
    onProjectWsResync: vi.fn(),
    onEmbeddingDegraded: vi.fn(),
    onEmbeddingRecovered: vi.fn(),
    onProjectUpdated: vi.fn(),
    onProjectDeleted: vi.fn(),
    onStatusChange: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

function createConnected(overrides: Partial<ProjectWsCallbacks> = {}) {
  const cb = makeCallbacks(overrides);
  const conn = new ProjectConnection('proj-1', cb);
  conn.connect();
  const ws = MockWebSocket.latest();
  ws.simulateOpen();
  ws.simulateMessage({ type: 'init' });
  return { conn, ws, cb };
}

// ── Setup ────────────────────────────────────────────────────────────────────

beforeEach(() => {
  MockWebSocket.reset();
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.useFakeTimers();
  // Reconnect delay carries up to 500ms jitter (anti-thundering-herd). Pin
  // Math.random to 0 so the exact-timing backoff assertions stay deterministic.
  vi.spyOn(Math, 'random').mockReturnValue(0);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('ProjectConnection lifecycle', () => {
  it('does not create WS in constructor', () => {
    const conn = new ProjectConnection('proj-1', makeCallbacks());
    expect(MockWebSocket.instances).toHaveLength(0);
    conn.disconnect();
  });

  it('creates WS with correct URL on connect()', () => {
    const conn = new ProjectConnection('proj-1', makeCallbacks());
    conn.connect();
    const ws = MockWebSocket.latest();
    expect(ws.url).toContain('/ws/project/proj-1');
    conn.disconnect();
  });

  it('sets status to connecting initially', () => {
    const cb = makeCallbacks();
    const conn = new ProjectConnection('proj-1', cb);
    conn.connect();
    expect(cb.onStatusChange).toHaveBeenCalledWith('connecting');
    conn.disconnect();
  });

  it('sets status to connected on init message', () => {
    const { cb, conn } = createConnected();
    expect(cb.onStatusChange).toHaveBeenCalledWith('connected');
    conn.disconnect();
  });

  it('disconnect() closes WS and prevents reconnect', () => {
    const { conn, ws } = createConnected();
    conn.disconnect();
    ws.simulateClose();
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('does not create duplicate WS if already connected', () => {
    const conn = new ProjectConnection('proj-1', makeCallbacks());
    conn.connect();
    const ws = MockWebSocket.latest();
    ws.simulateOpen();
    conn.connect(); // second call — should be no-op
    expect(MockWebSocket.instances).toHaveLength(1);
    conn.disconnect();
  });
});

describe('ProjectConnection reconnection', () => {
  it('schedules reconnect on unintentional close', () => {
    const { cb, ws, conn } = createConnected();
    ws.simulateClose();
    expect(cb.onStatusChange).toHaveBeenCalledWith('reconnecting');
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
    conn.disconnect();
  });

  it('an open after a drop fires onProjectWsResync; the FIRST open does not (step 8)', () => {
    const { cb, ws, conn } = createConnected();
    expect(cb.onProjectWsResync).not.toHaveBeenCalled();
    ws.simulateClose();
    vi.advanceTimersByTime(1000);
    MockWebSocket.latest().simulateOpen();
    expect(cb.onProjectWsResync).toHaveBeenCalledTimes(1);
    conn.disconnect();
  });

  // ── FIX 9: honor auth close codes (4001/4003/4004) ────────────────────────

  it('4003 auth close sets offline and does NOT reconnect', () => {
    const onError = vi.fn();
    const { ws, conn } = createConnected({ onError });
    ws.simulateClose(4003, 'No access');
    expect(MockWebSocket.instances).toHaveLength(1); // no reconnect scheduled
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(onError).toHaveBeenCalled();
    conn.disconnect();
  });

  it('4001 auth close sets offline and does NOT reconnect', () => {
    const { ws, conn } = createConnected();
    ws.simulateClose(4001, 'Unauthorized');
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    conn.disconnect();
  });

  it('transient close (1006) still reconnects normally', () => {
    const { ws, conn } = createConnected();
    ws.simulateClose(1006);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
    conn.disconnect();
  });

  it('applies exponential backoff', () => {
    const { ws, conn } = createConnected();
    // First close → 1s delay
    ws.simulateClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);

    // Second close → 2s delay
    const ws2 = MockWebSocket.latest();
    ws2.simulateClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2); // not yet
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(3);

    conn.disconnect();
  });

  it('caps reconnect delay at 30s', () => {
    const { ws, conn } = createConnected();
    // Force many disconnects to push delay beyond 30s
    let currentWs = ws;
    for (let i = 0; i < 10; i++) {
      currentWs.simulateClose();
      vi.advanceTimersByTime(30_000);
      currentWs = MockWebSocket.latest();
      currentWs.simulateOpen();
    }
    // After 10 iterations, delay should still be at most 30s
    currentWs.simulateClose();
    vi.advanceTimersByTime(30_000);
    expect(MockWebSocket.instances.length).toBeGreaterThan(10);
    conn.disconnect();
  });

  it('resets backoff delay after successful connect', () => {
    const { ws, conn } = createConnected();
    // Disconnect and reconnect several times to increase delay
    ws.simulateClose();
    vi.advanceTimersByTime(1000);
    const ws2 = MockWebSocket.latest();
    ws2.simulateOpen();
    ws2.simulateMessage({ type: 'init' });

    // After successful init, delay should reset
    ws2.simulateClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(3);
    conn.disconnect();
  });
});

describe('ProjectConnection message dispatch', () => {
  it('dispatches document_created', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'document_created', document_id: 'd1', title: 'New', parent_id: 'p1', sort_key: 'a0' });
    expect(cb.onDocumentCreated).toHaveBeenCalledWith('d1', 'New', 'p1', 'a0');
    conn.disconnect();
  });

  it('dispatches document_created with null parent_id', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'document_created', document_id: 'd1', title: 'Root', parent_id: null });
    expect(cb.onDocumentCreated).toHaveBeenCalledWith('d1', 'Root', null, null);
    conn.disconnect();
  });

  it('dispatches document_renamed', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'document_renamed', document_id: 'd1', title: 'Renamed' });
    expect(cb.onDocumentRenamed).toHaveBeenCalledWith('d1', 'Renamed');
    conn.disconnect();
  });

  it('dispatches document_moved (forwards previous_parent_id)', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'document_moved', document_id: 'd1', parent_id: 'p2', sort_key: 'a1', previous_parent_id: 'p0' });
    expect(cb.onDocumentMoved).toHaveBeenCalledWith('d1', 'p2', 'a1', 'p0', undefined, null);
    conn.disconnect();
  });

  it('dispatches document_moved with the conversion kind + title', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({
      type: 'document_moved', document_id: 'd1', parent_id: 'p2',
      sort_key: null, previous_parent_id: 'p0',
      is_reference: true, title: 'Converted',
    });
    expect(cb.onDocumentMoved).toHaveBeenCalledWith('d1', 'p2', null, 'p0', true, 'Converted');
    conn.disconnect();
  });

  it('dispatches document_reordered', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'document_reordered', document_id: 'd1', parent_id: 'p1', sort_key: 'a0V' });
    expect(cb.onDocumentReordered).toHaveBeenCalledWith('d1', 'p1', 'a0V');
    conn.disconnect();
  });

  it('dispatches document_deleted', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'document_deleted', document_id: 'd1' });
    expect(cb.onDocumentDeleted).toHaveBeenCalledWith('d1');
    conn.disconnect();
  });

  it('dispatches reference_created', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_created', reference_id: 'r1', title: 'Ref', document_id: 'd1' });
    // Author fields default to null when absent (impersonal/widget-key creation).
    expect(cb.onReferenceCreated).toHaveBeenCalledWith('r1', 'Ref', 'd1', null, null);
    conn.disconnect();
  });

  it('dispatches reference_created with author fields', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({
      type: 'reference_created', reference_id: 'r2', title: 'Ref', document_id: 'd1',
      created_by: 'u1', created_by_name: 'Alice',
    });
    expect(cb.onReferenceCreated).toHaveBeenCalledWith('r2', 'Ref', 'd1', 'u1', 'Alice');
    conn.disconnect();
  });

  it('dispatches reference_renamed', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_renamed', reference_id: 'r1', title: 'New Name' });
    expect(cb.onReferenceRenamed).toHaveBeenCalledWith('r1', 'New Name');
    conn.disconnect();
  });

  it('dispatches reference_moved', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_moved', reference_id: 'r1', document_id: 'd2' });
    expect(cb.onReferenceMoved).toHaveBeenCalledWith('r1', 'd2');
    conn.disconnect();
  });

  it('dispatches reference_moved with null document_id', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_moved', reference_id: 'r1', document_id: null });
    expect(cb.onReferenceMoved).toHaveBeenCalledWith('r1', null);
    conn.disconnect();
  });

  it('dispatches reference_deleted', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_deleted', reference_id: 'r1' });
    expect(cb.onReferenceDeleted).toHaveBeenCalledWith('r1');
    conn.disconnect();
  });

  it('dispatches reference_updated', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_updated', reference_id: 'r1' });
    expect(cb.onReferenceUpdated).toHaveBeenCalledWith('r1');
    conn.disconnect();
  });

  it('dispatches reference_status_changed', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'reference_status_changed', reference_id: 'r1', status: 'ready' });
    expect(cb.onReferenceStatusChanged).toHaveBeenCalledWith('r1', 'ready');
    conn.disconnect();
  });

  it('dispatches content_flushed', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'content_flushed', entity_id: 'd1', entity_type: 'doc' });
    expect(cb.onContentFlushed).toHaveBeenCalledWith('d1', 'doc');
    conn.disconnect();
  });

  it('dispatches generate_image_progress', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'generate_image_progress', session_id: 's1', phase: 'generating', run_id: 'r1', message_id: 'm1' });
    expect(cb.onGenerateImageProgress).toHaveBeenCalledWith('s1', 'generating', 'r1', 'm1');
    conn.disconnect();
  });

  it('dispatches generate_image_done with validated fields', () => {
    const { ws, cb, conn } = createConnected();
    const steps = [
      { tool_call_id: 'gen:r1:refine', tool: 'refine_prompt', summary: 'refine prompt', detail: 'REFINED' },
      { tool_call_id: 'gen:r1', tool: 'generate_image', summary: 'generate image', image_ref_ids: ['ref-a', 'ref-b'], run_id: 'r1' },
    ];
    ws.simulateMessage({
      type: 'generate_image_done', session_id: 's1', run_id: 'r1', message_id: 'm1',
      reference_ids: ['ref-a', 'ref-b'], title: 'a cat',
      refine: { prompt: 'REFINED', ok: true, error: null },
      steps,
    });
    expect(cb.onGenerateImageDone).toHaveBeenCalledWith({
      sessionId: 's1', runId: 'r1', messageId: 'm1',
      referenceIds: ['ref-a', 'ref-b'], title: 'a cat',
      refine: { prompt: 'REFINED', ok: true, error: null },
      steps,
    });
    conn.disconnect();
  });

  it('dispatches generate_image_failed with validated fields', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({
      type: 'generate_image_failed', session_id: 's1', run_id: 'r1',
      message_id: 'm1', error: 'ComfyUI is not available',
    });
    expect(cb.onGenerateImageFailed).toHaveBeenCalledWith({
      sessionId: 's1', runId: 'r1', messageId: 'm1', error: 'ComfyUI is not available',
    });
    conn.disconnect();
  });

  it('dispatches project_updated', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'project_updated', name: 'New Name' });
    expect(cb.onProjectUpdated).toHaveBeenCalledWith(expect.objectContaining({ name: 'New Name' }));
    conn.disconnect();
  });

  it('dispatches project_deleted', () => {
    const { ws, cb, conn } = createConnected();
    ws.simulateMessage({ type: 'project_deleted' });
    expect(cb.onProjectDeleted).toHaveBeenCalled();
    conn.disconnect();
  });

  it('ignores unknown message types without error', () => {
    const { ws, conn } = createConnected();
    expect(() => {
      ws.simulateMessage({ type: 'unknown_event', data: 'foo' });
    }).not.toThrow();
    conn.disconnect();
  });

  it('ignores malformed JSON without error', () => {
    const { ws, conn } = createConnected();
    expect(() => {
      ws.onmessage?.({ data: 'not json' });
    }).not.toThrow();
    conn.disconnect();
  });

  it('dispatches extraction_error with all required fields', () => {
    const onExtractionError = vi.fn();
    const { ws, conn } = createConnected({ onExtractionError });
    ws.simulateMessage({
      type: 'extraction_error',
      reference_id: 'r1',
      note_id: 'n1',
      document_id: 'd1',
    });
    expect(onExtractionError).toHaveBeenCalledWith('r1', 'n1', 'd1');
    conn.disconnect();
  });

  it('ignores extraction_error when a required field is missing', () => {
    const onExtractionError = vi.fn();
    const { ws, conn } = createConnected({ onExtractionError });
    ws.simulateMessage({ type: 'extraction_error', reference_id: 'r1', note_id: 'n1' });
    expect(onExtractionError).not.toHaveBeenCalled();
    conn.disconnect();
  });

  it('dispatches embedding_degraded and embedding_recovered', () => {
    const onEmbeddingDegraded = vi.fn();
    const onEmbeddingRecovered = vi.fn();
    const { ws, conn } = createConnected({ onEmbeddingDegraded, onEmbeddingRecovered });
    ws.simulateMessage({ type: 'embedding_degraded' });
    ws.simulateMessage({ type: 'embedding_recovered' });
    expect(onEmbeddingDegraded).toHaveBeenCalledTimes(1);
    expect(onEmbeddingRecovered).toHaveBeenCalledTimes(1);
    conn.disconnect();
  });

  it('dispatches documents_deleted_batch', () => {
    const onDocumentsDeletedBatch = vi.fn();
    const { ws, conn } = createConnected({ onDocumentsDeletedBatch });
    ws.simulateMessage({
      type: 'documents_deleted_batch',
      document_ids: ['d1', 'd2'],
      reference_ids: ['d2'],
    });
    expect(onDocumentsDeletedBatch).toHaveBeenCalledWith(['d1', 'd2'], ['d2']);
    conn.disconnect();
  });

  it('ignores documents_deleted_batch when document_ids is missing', () => {
    const onDocumentsDeletedBatch = vi.fn();
    const { ws, conn } = createConnected({ onDocumentsDeletedBatch });
    ws.simulateMessage({ type: 'documents_deleted_batch', reference_ids: [] });
    expect(onDocumentsDeletedBatch).not.toHaveBeenCalled();
    conn.disconnect();
  });

  it('dispatches documents_moved_out with the target project fields', () => {
    const onDocumentsMovedOut = vi.fn();
    const { ws, conn } = createConnected({ onDocumentsMovedOut });
    ws.simulateMessage({
      type: 'documents_moved_out',
      document_ids: ['d1'],
      reference_ids: ['r1'],
      target_project_id: 'p2',
      target_project_name: 'Target',
    });
    expect(onDocumentsMovedOut).toHaveBeenCalledWith(['d1'], ['r1'], 'p2', 'Target');
    conn.disconnect();
  });

  it('defaults documents_moved_out optional fields when absent', () => {
    const onDocumentsMovedOut = vi.fn();
    const { ws, conn } = createConnected({ onDocumentsMovedOut });
    ws.simulateMessage({ type: 'documents_moved_out', document_ids: ['d1'] });
    expect(onDocumentsMovedOut).toHaveBeenCalledWith(['d1'], [], '', '');
    conn.disconnect();
  });

  it('dispatches documents_moved_in', () => {
    const onDocumentsMovedIn = vi.fn();
    const { ws, conn } = createConnected({ onDocumentsMovedIn });
    ws.simulateMessage({ type: 'documents_moved_in', document_ids: ['d1', 'd2'] });
    expect(onDocumentsMovedIn).toHaveBeenCalledWith(['d1', 'd2']);
    conn.disconnect();
  });

  it('ignores documents_moved_in when document_ids is missing', () => {
    const onDocumentsMovedIn = vi.fn();
    const { ws, conn } = createConnected({ onDocumentsMovedIn });
    ws.simulateMessage({ type: 'documents_moved_in' });
    expect(onDocumentsMovedIn).not.toHaveBeenCalled();
    conn.disconnect();
  });
});

// ── chat_frame (plan agent-line-harness-lifecycle step 7) ────────────────────

describe('chat_frame dispatch', () => {
  it('forwards a verbatim frame with its session id to onChatFrame', () => {
    const { ws, cb } = createConnected();
    const frame = { type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' };
    ws.simulateMessage({ type: 'chat_frame', session_id: 's1', frame });
    expect(cb.onChatFrame).toHaveBeenCalledWith('s1', frame);
  });

  it('drops envelopes missing the session id or the frame object', () => {
    const { ws, cb } = createConnected();
    ws.simulateMessage({ type: 'chat_frame', frame: { type: 'ids' } });
    ws.simulateMessage({ type: 'chat_frame', session_id: 's1', frame: 'nope' });
    expect(cb.onChatFrame).not.toHaveBeenCalled();
  });
});
