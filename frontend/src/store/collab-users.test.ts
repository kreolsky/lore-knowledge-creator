// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { useAppStore } from './app-store';
import type { PresenceUser } from '../types';

const a: PresenceUser = { user_id: 'a', name: 'Alice', access_level: 'full' };
const b: PresenceUser = { user_id: 'b', name: 'Bob', access_level: 'full' };

describe('app-store collabUsers slice', () => {
  beforeEach(() => useAppStore.getState().setCollabUsers([]));

  it('replaces the list with setCollabUsers', () => {
    useAppStore.getState().setCollabUsers([a, b]);
    expect(useAppStore.getState().collabUsers).toEqual([a, b]);
  });

  it('adds a user without duplicating an existing one', () => {
    useAppStore.getState().setCollabUsers([a]);
    useAppStore.getState().addCollabUser(b);
    useAppStore.getState().addCollabUser(a);
    expect(useAppStore.getState().collabUsers).toEqual([a, b]);
  });

  it('removes a user by id', () => {
    useAppStore.getState().setCollabUsers([a, b]);
    useAppStore.getState().removeCollabUser('a');
    expect(useAppStore.getState().collabUsers).toEqual([b]);
  });

  it('resets to empty', () => {
    useAppStore.getState().setCollabUsers([a, b]);
    useAppStore.getState().setCollabUsers([]);
    expect(useAppStore.getState().collabUsers).toEqual([]);
  });
});
