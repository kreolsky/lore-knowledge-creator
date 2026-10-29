/** Tests for the local Y.Doc mirror — round trip, deletion, and the no-IndexedDB fallback. */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import 'fake-indexeddb/auto';
import * as Y from 'yjs';
import {
  attachLocalDoc,
  clearAllLocalDocs,
} from '../local-doc-persistence';

const PROJECT = 'proj1';
const ENTITY = 'doc1';

/** Attach a fresh doc to the same key and return what the local copy restored. */
async function readLocally(): Promise<string> {
  const ydoc = new Y.Doc();
  const handle = attachLocalDoc(PROJECT, ENTITY, ydoc)!;
  await handle.whenReady;
  const text = ydoc.getText('content').toString();
  await handle.detach();
  ydoc.destroy();
  return text;
}

describe('local-doc-persistence', () => {
  beforeEach(async () => {
    localStorage.clear();
    await clearAllLocalDocs();
  });
  afterEach(async () => {
    localStorage.clear();
    await clearAllLocalDocs();
  });

  it('drops a copy whose document is still open — and the live mirror stops writing back', async () => {
    // The shared-machine case that matters: the user logs out with a document open, so a
    // live IndexeddbPersistence still holds the database. Deleting without detaching it
    // leaves a writer attached, and the next keystroke stores the text right back.
    const ydoc = new Y.Doc();
    const handle = attachLocalDoc(PROJECT, ENTITY, ydoc)!;
    await handle.whenReady;
    ydoc.getText('content').insert(0, 'open while logging out');
    await handle.flush();

    await clearAllLocalDocs();
    expect(await readLocally()).toBe('');

    ydoc.getText('content').insert(0, 'typed after the wipe');
    await new Promise(r => setTimeout(r, 200));
    expect(await readLocally()).toBe('');

    ydoc.destroy();
  });

});
