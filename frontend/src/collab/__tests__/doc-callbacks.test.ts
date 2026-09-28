/** Tests for the doc-layer control frames (doc-callbacks) — doc lifecycle, notes, agent presence. */
import { describe, it, expect, vi } from 'vitest';
import type { DocCollabCallbacks } from '../doc-callbacks';
import { dispatchDocControlFrame } from '../doc-callbacks';

function makeBundles(): DocCollabCallbacks[] {
  return [{
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
    onAgentEditing: vi.fn(),
    onNoteSessionCreated: vi.fn(),
    onNoteSessionDeleted: vi.fn(),
    onNoteMessageChanged: vi.fn(),
  }];
}

describe('dispatchDocControlFrame (doc frames)', () => {
  it('doc_deleted / backlinks_changed / save_degraded / save_recovered broadcast when entity present', () => {
    const bundles = makeBundles();
    expect(dispatchDocControlFrame({ type: 'doc_deleted' }, bundles)).toBe(true);
    expect(dispatchDocControlFrame({ type: 'backlinks_changed' }, bundles)).toBe(true);
    expect(dispatchDocControlFrame({ type: 'save_degraded' }, bundles)).toBe(true);
    expect(dispatchDocControlFrame({ type: 'save_recovered' }, bundles)).toBe(true);
    expect(bundles[0].onDocDeleted).toHaveBeenCalledTimes(1);
    expect(bundles[0].onBacklinksChanged).toHaveBeenCalledTimes(1);
    expect(bundles[0].onSaveDegraded).toHaveBeenCalledTimes(1);
    expect(bundles[0].onSaveRecovered).toHaveBeenCalledTimes(1);
  });

  it('doc frames without an entity are consumed but broadcast nothing', () => {
    const bundles = makeBundles();
    expect(dispatchDocControlFrame({ type: 'doc_deleted' }, null)).toBe(true);
    expect(bundles[0].onDocDeleted).not.toHaveBeenCalled();
  });

  it('checkpoint_created / document_history_added require object payloads', () => {
    const bundles = makeBundles();
    expect(dispatchDocControlFrame({ type: 'checkpoint_created', checkpoint: { checkpoint_id: 'cp1' } }, bundles)).toBe(true);
    expect(bundles[0].onCheckpointCreated).toHaveBeenCalledWith({ checkpoint_id: 'cp1' });
    expect(dispatchDocControlFrame({ type: 'checkpoint_created', checkpoint: 'nope' }, bundles)).toBe(true);
    expect(bundles[0].onCheckpointCreated).toHaveBeenCalledTimes(1);
    expect(dispatchDocControlFrame({ type: 'document_history_added', event: { id: 1 } }, bundles)).toBe(true);
    expect(bundles[0].onDocumentHistoryAdded).toHaveBeenCalledWith({ id: 1 });
  });

  it('note_session_created accepts a full session AND the minimal {session_id} frame', () => {
    const bundles = makeBundles();
    const full = { session_id: 's1', title: 'Note' };
    expect(dispatchDocControlFrame({ type: 'note_session_created', session: full }, bundles)).toBe(true);
    expect(bundles[0].onNoteSessionCreated).toHaveBeenCalledWith(full);
    expect(dispatchDocControlFrame({ type: 'note_session_created', session_id: 's2' }, bundles)).toBe(true);
    expect(bundles[0].onNoteSessionCreated).toHaveBeenCalledWith({ session_id: 's2' });
    // A minimal frame without any resolvable id is dropped.
    expect(dispatchDocControlFrame({ type: 'note_session_created' }, bundles)).toBe(true);
    expect(bundles[0].onNoteSessionCreated).toHaveBeenCalledTimes(2);
  });

  it('note_session_deleted requires a string id; note_message_changed forwards the frame verbatim', () => {
    const bundles = makeBundles();
    expect(dispatchDocControlFrame({ type: 'note_session_deleted', session_id: 's1' }, bundles)).toBe(true);
    expect(bundles[0].onNoteSessionDeleted).toHaveBeenCalledWith('s1');
    expect(dispatchDocControlFrame({ type: 'note_session_deleted', session_id: 5 }, bundles)).toBe(true);
    expect(bundles[0].onNoteSessionDeleted).toHaveBeenCalledTimes(1);
    const frame = { type: 'note_message_changed', session_id: 's1', action: 'added' };
    expect(dispatchDocControlFrame(frame, bundles)).toBe(true);
    expect(bundles[0].onNoteMessageChanged).toHaveBeenCalledWith(frame);
  });

  it('agent_editing passes a non-string on_behalf_of as null', () => {
    const bundles = makeBundles();
    expect(dispatchDocControlFrame({ type: 'agent_editing', on_behalf_of: 'u1' }, bundles)).toBe(true);
    expect(bundles[0].onAgentEditing).toHaveBeenCalledWith('u1');
    expect(dispatchDocControlFrame({ type: 'agent_editing', on_behalf_of: 9 }, bundles)).toBe(true);
    expect(bundles[0].onAgentEditing).toHaveBeenCalledWith(null);
  });

  it('unknown frame types return false (not consumed)', () => {
    expect(dispatchDocControlFrame({ type: 'init' }, makeBundles())).toBe(false);
    expect(dispatchDocControlFrame({ type: 'user_left', user_id: 'u1' }, makeBundles())).toBe(false);
  });
});
