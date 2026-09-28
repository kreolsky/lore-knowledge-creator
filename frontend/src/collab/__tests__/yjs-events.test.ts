/** Tests for the generic entity-collab control frames (yjs-events) — presence/access/error dispatch + payload guards. */
import { describe, it, expect, vi } from 'vitest';
import type { YjsCollabCallbacks } from '../yjs-events';
import { dispatchEntityFrame } from '../yjs-events';

function makeBundles(): YjsCollabCallbacks[] {
  return [{
    onPresenceUsers: vi.fn(),
    onUserJoined: vi.fn(),
    onUserLeft: vi.fn(),
    onAccessChanged: vi.fn(),
    onAccessRevoked: vi.fn(),
    onStatusChange: vi.fn(),
    onSynced: vi.fn(),
    onError: vi.fn(),
  }];
}

describe('dispatchEntityFrame (generic frames)', () => {
  it('init broadcasts connected status + filtered presence users', () => {
    const bundles = makeBundles();
    const handled = dispatchEntityFrame({
      type: 'init',
      entity_id: 'e1',
      users: [
        { user_id: 'u1', name: 'A' },
        { user_id: 42, name: 'B' },          // dropped: user_id not a string
        { name: 'C' },                        // dropped: no user_id
        { user_id: 'u2', name: 'D' },
      ],
    }, bundles);
    expect(handled).toBe(true);
    expect(bundles[0].onStatusChange).toHaveBeenCalledWith('connected');
    expect(bundles[0].onPresenceUsers).toHaveBeenCalledTimes(1);
    const users = (bundles[0].onPresenceUsers as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(users.map((u: { user_id: string }) => u.user_id)).toEqual(['u1', 'u2']);
  });

  it('user_joined with a valid payload broadcasts; malformed payload is dropped silently', () => {
    const bundles = makeBundles();
    expect(dispatchEntityFrame({ type: 'user_joined', user: { user_id: 'u1', name: 'A' } }, bundles)).toBe(true);
    expect(bundles[0].onUserJoined).toHaveBeenCalledWith({ user_id: 'u1', name: 'A' });
    (bundles[0].onUserJoined as ReturnType<typeof vi.fn>).mockClear();
    expect(dispatchEntityFrame({ type: 'user_joined', user: { user_id: 7 } }, bundles)).toBe(true);
    expect(bundles[0].onUserJoined).not.toHaveBeenCalled();
  });

  it('access_changed requires a string level; access_revoked always broadcasts', () => {
    const bundles = makeBundles();
    expect(dispatchEntityFrame({ type: 'access_changed', level: 'full' }, bundles)).toBe(true);
    expect(bundles[0].onAccessChanged).toHaveBeenCalledWith('full');
    expect(dispatchEntityFrame({ type: 'access_changed', level: 3 }, bundles)).toBe(true);
    expect(bundles[0].onAccessChanged).toHaveBeenCalledTimes(1);
    expect(dispatchEntityFrame({ type: 'access_revoked' }, bundles)).toBe(true);
    expect(bundles[0].onAccessRevoked).toHaveBeenCalledTimes(1);
  });

  it('error without an entity logs to console instead of broadcasting', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const bundles = makeBundles();
    expect(dispatchEntityFrame({ type: 'error', message: 'boom' }, null)).toBe(true);
    expect(bundles[0].onError).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith('[yjs-provider] error:', 'boom');
    warn.mockRestore();
  });

  it('unknown frame types return false (not consumed)', () => {
    expect(dispatchEntityFrame({ type: 'doc_deleted' }, makeBundles())).toBe(false);
    expect(dispatchEntityFrame({ type: 'flush_ack', entity_id: 'e1' }, makeBundles())).toBe(false);
    expect(dispatchEntityFrame({ type: 'something_else' }, makeBundles())).toBe(false);
  });
});
