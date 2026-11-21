/**
 * Cross-project paste: images referenced by pasted Lore markdown are copied into the current project.
 *
 * A pasted `![x](ref:id)` / `[x](ref:id)` whose reference belongs to ANOTHER project would
 * render as a dead link here. After the paste lands, every such id the user can read is
 * copied in the browser — meta GET, the original file from the URL the source editor
 * rendered (immutable-cached, usually no network), and the ordinary upload into the paste
 * view's host document — then `(ref:old)` is rewritten to `(ref:new)` across the entity
 * ydoc (host text + anchored table cells) in ONE Replace-all step.
 *
 * ARCH: browser-side by design — the upload pipeline (type check, size limit, thumbnails,
 * events) and both access checks stay the server's existing ones; no copy endpoint.
 * WHY rewrite after the insert, over the whole ydoc: the paste stays synchronous, pasted
 * tables carry their text in cell Y.Texts, and a text-wide replace needs no position
 * mapped through concurrent edits. The old id is foreign to this project, so every
 * occurrence in this ydoc is a dead link — replacing all of them is correct.
 */

import type { EditorView } from '@codemirror/view';
import { SearchQuery } from '@codemirror/search';
import type * as Y from 'yjs';
import { apiClient } from '../api/client';
import { uploadReferenceFile } from '../api/reference-upload';
import { applyReplaceAll, planReplaceAll, type ReplaceAllPlan } from '../components/editor/find-replace-undo';
import { getEntityHandle } from '../collab/active-handle-registry';
import { useAppStore } from '../store/app-store';
import type { Reference } from '../types';
import { t } from '../i18n';
import { getViewEntity } from './active-editor';
import { collectRefIdsFromText } from './ref-ids';
import { viewHostDocument } from './upload-host';

const UPLOAD_CONCURRENCY = 3;

// WHY module-level: pasting the same content twice while its copies upload must not upload
// twice. Keyed by target host + source id; an entry lives until its copy settles.
const inflight = new Map<string, Promise<string>>();

/** Copy one foreign reference into `projectId` under `hostId`; resolves to the new id. */
async function copyReference(sourceId: string, projectId: string, hostId: string): Promise<string> {
  const meta = await apiClient.get(`/references/${encodeURIComponent(sourceId)}`) as Reference;
  // WHY: absent from the probe yet of THIS project = not live here (soft-deleted); a paste
  // must not resurrect it as a copy.
  if (meta.project_id === projectId) throw new Error('reference of this project is not live');
  // INVARIANT: only image references are copied; any other kind keeps its link with a toast.
  // Why: operator ruling — a PDF reference is a converted markdown document the image/audio
  // upload refuses (400), and an audio copy re-runs transcription on the shared AI host.
  if (meta.media_type !== 'image') throw new Error(`not an image reference (${meta.media_type})`);
  const name = meta.file_meta?.original_name;
  if (!name) throw new Error('not a file reference');
  // WHY this exact URL: it is the one the source editor rendered (useEditorReferenceSync
  // imageRefUrl), served `immutable`, so the bytes usually come from the HTTP cache.
  const resp = await fetch(`/api/files/${sourceId}/${name}`, { credentials: 'include' });
  if (!resp.ok) throw new Error(`file ${resp.status}`);
  const blob = await resp.blob();
  const ref = await uploadReferenceFile(projectId, hostId, new File([blob], name, { type: blob.type }));
  return ref.reference_id;
}

function copyOnce(sourceId: string, projectId: string, hostId: string): Promise<string> {
  const key = `${hostId}\u0000${sourceId}`;
  const existing = inflight.get(key);
  if (existing) return existing;
  const p = copyReference(sourceId, projectId, hostId).finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

/** Run `fn` over `items` with at most `limit` in flight; results in input order. */
async function mapLimited<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<PromiseSettledResult<R>[]> {
  const out: PromiseSettledResult<R>[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      try { out[i] = { status: 'fulfilled', value: await fn(items[i]) }; }
      catch (reason) { out[i] = { status: 'rejected', reason }; }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

/** One Replace-all plan rewriting every `(ref:old)` to `(ref:new)` — host + cells merged. */
function planRewrite(ydoc: Y.Doc, renames: [string, string][]): ReplaceAllPlan {
  const host = ydoc.getText('content').toString();
  const merged: ReplaceAllPlan = { host: [], cells: new Map(), cellCount: 0 };
  for (const [from, to] of renames) {
    const query = new SearchQuery({ search: `(ref:${from})`, replace: `(ref:${to})`, literal: true, caseSensitive: true });
    const plan = planReplaceAll(query, host, ydoc);
    merged.host.push(...plan.host);
    for (const [key, cell] of plan.cells) {
      const prev = merged.cells.get(key);
      if (prev) prev.edits.push(...cell.edits);
      else merged.cells.set(key, { text: cell.text, edits: [...cell.edits] });
    }
    merged.cellCount += plan.cellCount;
  }
  // WHY sorted: applyReplaceAll applies each text's edits last-first; ids differ, so the
  // literal matches of different renames never overlap.
  merged.host.sort((a, b) => a.from - b.from);
  for (const cell of merged.cells.values()) cell.edits.sort((a, b) => a.from - b.from);
  return merged;
}

/**
 * After a Lore-origin paste of `source` into `view`: copy every image reference of another
 * project into this one and point the pasted links at the copies. Fire-and-forget; every
 * outcome other than full success ends in a toast (the link stays as pasted).
 */
export function copyForeignRefs(view: EditorView, source: string): Promise<void> {
  return copyForeign(view, source).catch((err: unknown) => {
    console.warn('[paste] cross-project file copy failed', err);
    const count = collectRefIdsFromText(source).size;
    useAppStore.getState().showToast(t('pasteFilesCopyFailed', { count: String(count) }), 'error');
  });
}

async function copyForeign(view: EditorView, source: string): Promise<void> {
  const ids = [...collectRefIdsFromText(source)];
  const store = useAppStore.getState();
  const project = store.currentProject;
  if (ids.length === 0 || !project) return;
  const found = await apiClient.post('/references/resolve', { project_id: project.project_id, ids }) as Reference[];
  const own = new Set(found.map(r => r.reference_id));
  const foreign = ids.filter(id => !own.has(id));
  if (foreign.length === 0) return;

  const fail = () => store.showToast(t('pasteFilesCopyFailed', { count: String(foreign.length) }), 'error');
  const entityId = getViewEntity(view);
  const ydoc = entityId ? getEntityHandle(entityId)?.ydoc : undefined;
  const host = viewHostDocument(view, null);
  // WHY refused, not guessed: without the view's own entity/ydoc/host the copy could land
  // in another column's document or project (the image-drop host rule).
  if (!entityId || !ydoc || !host || host.project_id !== project.project_id) { fail(); return; }

  store.showToast(t('pasteFilesCopying', { count: String(foreign.length) }), 'info');
  const results = await mapLimited(foreign, UPLOAD_CONCURRENCY,
    (id) => copyOnce(id, project.project_id, host.document_id));
  const renames: [string, string][] = [];
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') renames.push([foreign[i], r.value]);
    else console.warn('[paste] reference copy failed', foreign[i], r.reason);
  });
  const failedCount = foreign.length - renames.length;

  // INVARIANT: the rewrite lands only in the ydoc that is STILL the entity's live handle.
  // Why: a closed document's ydoc is detached — a write there is silently lost, so the
  // user is told the copies exist but the links were not updated.
  if (renames.length && getEntityHandle(entityId)?.ydoc !== ydoc) {
    store.showToast(t('pasteFilesLinksNotUpdated'), 'error');
    return;
  }
  if (renames.length) applyReplaceAll(ydoc, ydoc.getText('content'), planRewrite(ydoc, renames))?.destroy();
  if (failedCount) store.showToast(t('pasteFilesCopyFailed', { count: String(failedCount) }), 'error');
  else store.showToast(t('pasteFilesCopied', { count: String(renames.length) }), 'info');
}
