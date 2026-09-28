/**
 * Deterministic UI-prefs seeding for e2e.
 *
 * ARCH: the right panel's open/active-tab state is per-document and persisted
 * server-side (user_preferences, see backend/routes/preferences.py). Across runs
 * the restored tab/open-state varies per doc, which made specs that *click* the
 * tab bar flake on actionability and on "is chat already the active tab?". Instead
 * of driving the UI, we PUT the prefs blob so the chat tab is open on first paint —
 * no clicking, no auto-restore guesswork.
 *
 * The blob is a FLEXIBLE pass-through; we GET → merge the target docs → PUT so we
 * never clobber unrelated keys.
 */
import type { Page } from '@playwright/test';

interface DocPref {
  rightPanelOpen?: boolean;
  rightPanelTab?: string;
  [k: string]: unknown;
}
interface Prefs {
  documents?: Record<string, DocPref>;
  [k: string]: unknown;
}

/** Force `rightPanelOpen=true` + `rightPanelTab=tab` for each doc in `docIds`. */
export async function seedTabOpen(
  page: Page,
  projectId: string,
  docIds: string[],
  tab: string,
): Promise<void> {
  const prefs = (await (await page.request.get(`/api/preferences/${projectId}`)).json()) as Prefs;
  const documents: Record<string, DocPref> = { ...(prefs.documents ?? {}) };
  for (const docId of docIds) {
    documents[docId] = { ...(documents[docId] ?? {}), rightPanelOpen: true, rightPanelTab: tab };
  }
  const next: Prefs = { ...prefs, documents };
  const res = await page.request.put(`/api/preferences/${projectId}`, {
    data: { preferences: next },
  });
  if (!res.ok()) {
    throw new Error(`seedTabOpen(${tab}): PUT /api/preferences/${projectId} → ${res.status()}`);
  }
}

/** Force the chat tab open for each doc in `docIds`. */
export function seedChatTabOpen(page: Page, projectId: string, docIds: string[]): Promise<void> {
  return seedTabOpen(page, projectId, docIds, 'chat');
}

/**
 * Pin (or clear) the project-level last-active chat BEFORE navigation, so the
 * open/restore path is deterministic instead of depending on what a previous
 * run or manual session left persisted.
 */
export async function seedLastActiveChat(
  page: Page,
  projectId: string,
  sessionId: string | null,
): Promise<void> {
  const prefs = (await (await page.request.get(`/api/preferences/${projectId}`)).json()) as Prefs;
  const next: Prefs = { ...prefs, lastActiveChatSessionId: sessionId };
  const res = await page.request.put(`/api/preferences/${projectId}`, {
    data: { preferences: next },
  });
  if (!res.ok()) {
    throw new Error(`seedLastActiveChat: PUT /api/preferences/${projectId} → ${res.status()}`);
  }
}

/** Force the references tab open for each doc in `docIds`. */
export function seedRefsTabOpen(page: Page, projectId: string, docIds: string[]): Promise<void> {
  return seedTabOpen(page, projectId, docIds, 'refs');
}

/**
 * Force the docs sidebar open and the whole tree expanded (collapsedDocIds=[]),
 * so EVERY document's `[data-doc-id]` row renders. Required by specs that drive
 * IN-APP navigation by clicking a tree row — a doc nested under a collapsed parent
 * has no row to click. Merges over existing keys.
 */
export async function seedTreeExpanded(page: Page, projectId: string): Promise<void> {
  const prefs = (await (await page.request.get(`/api/preferences/${projectId}`)).json()) as Prefs;
  const next: Prefs = { ...prefs, sidebarOpen: true, sidebarTab: 'docs', collapsedDocIds: [] };
  const res = await page.request.put(`/api/preferences/${projectId}`, {
    data: { preferences: next },
  });
  if (!res.ok()) {
    throw new Error(`seedTreeExpanded: PUT /api/preferences/${projectId} → ${res.status()}`);
  }
}
