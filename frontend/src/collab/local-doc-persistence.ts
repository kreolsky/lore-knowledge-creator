/**
 * Per-entity Y.Doc mirror in the browser's IndexedDB.
 *
 * The Y.Doc lives in tab memory only (see SYSTEM: collab). During a long outage
 * — 15% of measured outages run past 30s — the tab gets closed and everything
 * typed since the drop is gone. The mirror is what survives that close: reopening
 * the document restores the local state, and the collab sync carries it up.
 *
 * // SYSTEM: local-doc-persistence — IndexedDB mirror of each entity's Y.Doc
 * // ARCH: one IndexedDB database per project+entity, written by y-indexeddb.
 * //       Per-entity, not per-project, so a single revoked/deleted entity is
 * //       dropped without touching the rest of the project's copies.
 */

import type * as Y from 'yjs';
import { IndexeddbPersistence, clearDocument, storeState } from 'y-indexeddb';
import { registerLogoutHandler } from '../store/logout-handlers';

const DB_PREFIX = 'lore-doc:';
const PROBE_DB = `${DB_PREFIX}probe`;
/** localStorage key holding `{ [dbName]: lastOpenedMs }` — the sweeper's ledger. */
const INDEX_KEY = 'lore-doc-mirrors';
/** A mirror nobody has opened for this long is not an outage copy anyone is coming back for. */
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

/** Database name for one entity's mirror. The prefix is what clearAllLocalDocs matches. */
export function localDocDbName(projectId: string, entityId: string): string {
  return `${DB_PREFIX}${projectId}:${entityId}`;
}

export interface LocalDocHandle {
  /**
   * Resolves once the stored state has been applied to the Y.Doc.
   *
   * INVARIANT: this settles — it never rejects and never hangs. Why: the collab join
   * waits on it, so a browser that refuses IndexedDB (private mode, quota, a corrupt
   * store) would otherwise leave the document permanently unjoined; the mirror's failure
   * would become an editor outage. A dead mirror costs the offline safety net, nothing else.
   */
  whenReady: Promise<void>;
  /**
   * True for updates the mirror itself replayed into the doc. The provider skips those on
   * its update observer — the restored state reaches the server as the sync step2, not as
   * a stream of replayed updates.
   */
  isOwnOrigin: (origin: unknown) => boolean;
  /** Force everything applied so far out to IndexedDB and wait for the write. */
  flush: () => Promise<void>;
  /**
   * Stop mirroring, KEEP the data. Deletion goes through clearLocalDoc and its siblings,
   * which detach first — see the invariant on liveMirrors.
   */
  detach: () => Promise<void>;
}

function hasIndexedDb(): boolean {
  return typeof indexedDB !== 'undefined' && indexedDB !== null;
}

/** Report and swallow — every mirror operation is best-effort by the invariant above. */
function bestEffort<T>(p: Promise<T>, what: string): Promise<void> {
  return p.then(() => undefined).catch((e) => {
    // WHY: log only — the mirror is a local copy; the server document is the source of truth.
    console.error(`local document mirror: ${what} failed`, e);
  });
}

/**
 * Every mirror currently attached to a live Y.Doc, by database name.
 *
 * INVARIANT(security): a deletion path detaches the live handle BEFORE deleting the
 * database. Why: `deleteDatabase` against an open connection fires `onblocked` and does
 * not complete, and a provider still attached to a live Y.Doc stores the text again on
 * its next tick — the deletion would report success and leave the data. In the app the
 * logout navigation happens to tear the provider down first, so this is the module's own
 * guarantee rather than a reproduced leak: any caller may delete a mirror that is open.
 */
const liveMirrors = new Map<string, LocalDocHandle>();

/** Detach — without deleting — every live mirror whose database name matches. */
async function detachMatching(matches: (name: string) => boolean): Promise<void> {
  const targets = Array.from(liveMirrors.entries()).filter(([name]) => matches(name));
  await Promise.all(targets.map(([, handle]) => handle.detach()));
}

let probedFactory: IDBFactory | null = null;
let probeResult: Promise<boolean> | null = null;

/**
 * Can this browser actually give us a database?
 *
 * WHY: probe before constructing the y-indexeddb provider rather than reacting to its
 * failure. Why: that provider chains off its own open promise internally, and when the
 * open fails those chains reject with nobody holding them — an unhandled rejection we
 * cannot reach from here, on top of a whenSynced that never settles at all. A throwaway
 * database answers the same question with a promise we own. Cached per factory, so it
 * costs one open per session.
 */
function indexedDbUsable(): Promise<boolean> {
  if (probeResult === null || probedFactory !== indexedDB) {
    probedFactory = indexedDB;
    probeResult = new Promise<boolean>((resolve) => {
      let request: IDBOpenDBRequest;
      try {
        request = indexedDB.open(PROBE_DB);
      } catch {
        resolve(false);
        return;
      }
      request.onsuccess = () => {
        request.result.close();
        indexedDB.deleteDatabase(PROBE_DB);
        resolve(true);
      };
      request.onerror = () => resolve(false);
      request.onblocked = () => resolve(false);
    });
  }
  return probeResult;
}

function readIndex(): Record<string, number> {
  try {
    const raw = localStorage.getItem(INDEX_KEY);
    const parsed: unknown = raw === null ? null : JSON.parse(raw);
    return parsed !== null && typeof parsed === 'object' ? (parsed as Record<string, number>) : {};
  } catch {
    return {};
  }
}

function writeIndex(index: Record<string, number>): void {
  try {
    localStorage.setItem(INDEX_KEY, JSON.stringify(index));
  } catch {
    // A full or disabled localStorage costs the sweeper, not the mirror.
  }
}

let swept = false;

/**
 * Delete mirrors nobody is coming back for: too old, or not in the ledger at all.
 *
 * ARCH: the primary cleanup is deletion on a clean exit (see _dropMirrorIfFullySynced in
 * yjs-provider) — a mirror whose document ended synced has no job left. This sweep is the
 * backstop for what that path cannot reach: a tab killed mid-write, a browser that
 * dropped the unload handler, a mirror written by a build that predates the ledger.
 * Runs once per session, on the first attach.
 */
export async function sweepLocalDocs(): Promise<void> {
  if (!hasIndexedDb() || typeof indexedDB.databases !== 'function') return;
  const index = readIndex();
  const cutoff = Date.now() - MAX_AGE_MS;
  const names = (await indexedDB.databases())
    .map(d => d.name)
    .filter((n): n is string => typeof n === 'string' && n.startsWith(DB_PREFIX) && n !== PROBE_DB);

  const stale = names.filter(n => !(n in index) || index[n] < cutoff);
  await Promise.all(stale.map(n => bestEffort(clearDocument(n), 'sweep')));

  // Keep the ledger to what actually exists, so it cannot grow without bound either.
  const kept = new Set(names.filter(n => !stale.includes(n)));
  writeIndex(Object.fromEntries(Object.entries(index).filter(([n]) => kept.has(n))));
}

/**
 * Start mirroring `ydoc` under project+entity.
 *
 * Returns null when the browser has no IndexedDB at all (SSR, an old browser). A browser
 * that HAS the API but refuses to open a database returns a handle whose whenReady still
 * settles — see the invariant on LocalDocHandle; either way the editor works exactly as it
 * did before the mirror existed, it just has nothing to survive a closed tab with.
 */
export function attachLocalDoc(projectId: string, entityId: string, ydoc: Y.Doc): LocalDocHandle | null {
  if (!hasIndexedDb()) return null;

  const name = localDocDbName(projectId, entityId);
  let persistence: IndexeddbPersistence | null = null;

  // Stamp BEFORE sweeping, so the document being opened right now can never be swept.
  writeIndex({ ...readIndex(), [name]: Date.now() });
  if (!swept) {
    swept = true;
    void sweepLocalDocs();
  }

  const whenReady = bestEffort(
    indexedDbUsable().then((usable) => {
      if (!usable) {
        console.error(`local document mirror: IndexedDB unavailable, ${name} will not be kept`);
        return;
      }
      persistence = new IndexeddbPersistence(name, ydoc);
      return persistence.whenSynced;
    }),
    'load',
  );

  const handle: LocalDocHandle = {
    whenReady,
    isOwnOrigin: (origin) => persistence !== null && origin === persistence,
    flush: () => (persistence === null ? Promise.resolve() : bestEffort(storeState(persistence, true), 'flush')),
    detach: async () => {
      liveMirrors.delete(name);
      if (persistence !== null) await bestEffort(persistence.destroy(), 'detach');
    },
  };
  liveMirrors.set(name, handle);
  return handle;
}

/** Drop a name from the ledger — its database is gone. */
function forget(name: string): void {
  const index = readIndex();
  if (name in index) {
    delete index[name];
    writeIndex(index);
  }
}

/** Delete one entity's mirror — used when no handle is attached (entity deleted elsewhere). */
export async function clearLocalDoc(projectId: string, entityId: string): Promise<void> {
  if (!hasIndexedDb()) return;
  const name = localDocDbName(projectId, entityId);
  await detachMatching(n => n === name);
  await bestEffort(clearDocument(name), 'clear');
  forget(name);
}

/**
 * Delete every mirror belonging to one project.
 *
 * INVARIANT(security): the unit of an access refusal is the PROJECT, not the document the
 * user happened to have open. Why: a user whose access was revoked never opens the document
 * again — the app refuses it — so nothing per-entity ever runs, and a live drive found the
 * revoked text still readable straight out of IndexedDB
 * (`" CONTROL-X- OUTAGE-B-DURING SECRET-DURING-OUTAGE"`).
 */
export async function clearProjectLocalDocs(projectId: string): Promise<void> {
  if (!hasIndexedDb() || typeof indexedDB.databases !== 'function') return;
  const prefix = `${DB_PREFIX}${projectId}:`;
  await detachMatching(n => n.startsWith(prefix));
  const dbs = await indexedDB.databases();
  await Promise.all(
    dbs
      .map(d => d.name)
      .filter((n): n is string => typeof n === 'string' && n.startsWith(prefix))
      .map(async (n) => { await bestEffort(clearDocument(n), 'clear'); forget(n); }),
  );
}

/**
 * Delete an entity's mirror without knowing its project — the bare `/docs/:id` route
 * learns the refusal before it ever learns which project the document belongs to.
 */
export async function clearLocalDocsForEntity(entityId: string): Promise<void> {
  if (!hasIndexedDb() || typeof indexedDB.databases !== 'function') return;
  await detachMatching(n => n.endsWith(`:${entityId}`));
  const dbs = await indexedDB.databases();
  await Promise.all(
    dbs
      .map(d => d.name)
      .filter((n): n is string => typeof n === 'string' && n.startsWith(DB_PREFIX) && n.endsWith(`:${entityId}`))
      .map(async (n) => { await bestEffort(clearDocument(n), 'clear'); forget(n); }),
  );
}

/**
 * Delete every mirror this app wrote. Called on logout — a shared machine must not keep
 * the previous user's document text readable in the next session.
 */
export async function clearAllLocalDocs(): Promise<void> {
  if (!hasIndexedDb() || typeof indexedDB.databases !== 'function') return;
  await detachMatching(() => true);
  const dbs = await indexedDB.databases();
  await Promise.all(
    dbs
      .map(d => d.name)
      .filter((n): n is string => typeof n === 'string' && n.startsWith(DB_PREFIX) && n !== PROBE_DB)
      .map(n => bestEffort(clearDocument(n), 'clear')),
  );
  writeIndex({});
}

// INVARIANT(security): a soft logout deletes every mirror. Why: the mirror is document
// text at rest on the machine, and the next user in the same browser profile must not be
// able to read the previous user's project content out of it.
registerLogoutHandler(() => {
  void clearAllLocalDocs();
});
