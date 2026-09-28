/** Tests for the local Y.Doc mirror — round trip, deletion, and the no-IndexedDB fallback. */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import 'fake-indexeddb/auto';
import * as Y from 'yjs';
import {
  attachLocalDoc,
  clearLocalDoc,
  clearAllLocalDocs,
  sweepLocalDocs,
  localDocDbName,
} from '../local-doc-persistence';
import { clearUserScopedCaches } from '../../store/logout-handlers';

const PROJECT = 'proj1';
const ENTITY = 'doc1';

/** Write `text` into a freshly attached doc and wait for it to reach IndexedDB. */
async function writeLocally(text: string): Promise<void> {
  const ydoc = new Y.Doc();
  const handle = attachLocalDoc(PROJECT, ENTITY, ydoc)!;
  await handle.whenReady;
  ydoc.getText('content').insert(0, text);
  await handle.flush();
  await handle.detach();
  ydoc.destroy();
}

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

  it('restores the text a closed tab left behind', async () => {
    await writeLocally('typed during the outage');
    expect(await readLocally()).toBe('typed during the outage');
  });

  it('serves nothing after the local copy is cleared', async () => {
    await writeLocally('revoked content');
    await clearLocalDoc(PROJECT, ENTITY);
    expect(await readLocally()).toBe('');
  });

  it('clearAllLocalDocs drops our copies and leaves foreign databases alone', async () => {
    await writeLocally('mine');
    const foreign = await new Promise<IDBDatabase>((resolve) => {
      const req = indexedDB.open('someone-elses-db', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('s');
      req.onsuccess = () => resolve(req.result);
    });
    foreign.close();

    await clearAllLocalDocs();

    const names = (await indexedDB.databases()).map(d => d.name);
    expect(names).toContain('someone-elses-db');
    expect(names).not.toContain(localDocDbName(PROJECT, ENTITY));
  });

  it('returns null when the browser has no IndexedDB', () => {
    const real = globalThis.indexedDB;
    // @ts-expect-error — simulating a browser/private mode without IndexedDB
    delete globalThis.indexedDB;
    try {
      expect(attachLocalDoc(PROJECT, ENTITY, new Y.Doc())).toBeNull();
    } finally {
      globalThis.indexedDB = real;
    }
  });

  it('drops every local copy on logout — a shared machine keeps no readable text', async () => {
    await writeLocally('previous user text');

    clearUserScopedCaches();

    // The registered handler is fire-and-forget; poll until the deletion lands.
    const deadline = Date.now() + 5000;
    while ((await readLocally()) !== '') {
      if (Date.now() > deadline) throw new Error('local copy survived logout');
      await new Promise(r => setTimeout(r, 25));
    }
    expect(await readLocally()).toBe('');
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

  it('sweeps a mirror nobody has opened in a month, and keeps a fresh one', async () => {
    await writeLocally('old copy');
    const otherDoc = new Y.Doc();
    const fresh = attachLocalDoc(PROJECT, 'doc2', otherDoc)!;
    await fresh.whenReady;
    otherDoc.getText('content').insert(0, 'fresh copy');
    await fresh.flush();
    await fresh.detach();

    // Age the first entry past the 30-day cutoff in the ledger the sweeper reads.
    const index = JSON.parse(localStorage.getItem('lore-doc-mirrors') ?? '{}');
    index[localDocDbName(PROJECT, ENTITY)] = Date.now() - 31 * 24 * 60 * 60 * 1000;
    localStorage.setItem('lore-doc-mirrors', JSON.stringify(index));

    await sweepLocalDocs();

    const names = (await indexedDB.databases()).map(d => d.name);
    expect(names).not.toContain(localDocDbName(PROJECT, ENTITY));
    expect(names).toContain(localDocDbName(PROJECT, 'doc2'));
  });

  it('sweeps a mirror the ledger never knew about', async () => {
    await writeLocally('orphan copy');
    // A mirror written before the ledger existed, or by a tab that died mid-write.
    localStorage.removeItem('lore-doc-mirrors');

    await sweepLocalDocs();

    const names = (await indexedDB.databases()).map(d => d.name);
    expect(names).not.toContain(localDocDbName(PROJECT, ENTITY));
  });

});
