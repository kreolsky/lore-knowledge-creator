/** e2e scope acquisition: DEDICATED find-or-create project (deterministic shape on
 * every stand), never a skip — see the INVARIANT above and the ARCH notes on the
 * ensure* helpers. */
import type { Page } from '@playwright/test';

export interface DocScope {
  docId: string;
  sessionId: string;
}
export interface ChatScope {
  projectId: string;
  docA: DocScope;
  docB: DocScope;
}

/**
 * INVARIANT: e2e scope helpers never yield a null scope that throttles a spec into
 * skipping. Why: `test.skip(scope === null)` turns a missing data shape into a
 * silently-green run — the no-failing-branch failure (.claude/rules/testing.md).
 * Every ensure* resolves against one DEDICATED, clearly-titled project
 * (find-or-create, idempotent across runs), so the spec always executes on the
 * same shape — regardless of stand data or spec run order.
 */
const E2E_PROJECT_TITLE = '[e2e-scope] scratch';
const E2E_DOC_A_TITLE = '[e2e-scope] doc A';
const E2E_DOC_B_TITLE = '[e2e-scope] doc B';

interface ProjectDetail {
  project?: { owner_id?: string; name?: string };
  documents?: Array<{ document_id: string; title: string }>;
}

/**
 * Find or create the DEDICATED e2e scratch project (owned by the authenticated
 * user) with two titled documents. Idempotent by title — reused across runs so it
 * does not proliferate, and never touches a user's real project.
 * Throws on failure (the caller must FAIL, not skip).
 */
export async function ensureE2eProject(
  page: Page,
): Promise<{ projectId: string; docA: string; docB: string }> {
  const me = (await (await page.request.get('/api/auth/me')).json()) as { user_id: string };
  const projects = (await (await page.request.get('/api/projects')).json()) as Array<{
    project_id: string;
  }>;
  let projectId: string | null = null;
  let docs: Array<{ document_id: string; title: string }> = [];
  for (const p of projects) {
    const detail = (await (await page.request.get(`/api/projects/${p.project_id}`)).json()) as ProjectDetail;
    if (detail.project?.name === E2E_PROJECT_TITLE && detail.project.owner_id === me.user_id) {
      projectId = p.project_id;
      docs = detail.documents ?? [];
      break;
    }
  }
  if (projectId === null) {
    const res = await page.request.post('/api/projects', { data: { name: E2E_PROJECT_TITLE } });
    if (!res.ok()) {
      throw new Error(`ensureE2eProject: POST /api/projects → ${res.status()} ${await res.text()}`);
    }
    projectId = ((await res.json()) as { project_id: string }).project_id;
  }
  const ensureDoc = async (title: string): Promise<string> => {
    const existing = docs.find((d) => d.title === title);
    if (existing) return existing.document_id;
    const res = await page.request.post('/api/documents', {
      data: { project_id: projectId, title, content: '' },
    });
    if (!res.ok()) {
      throw new Error(`ensureE2eProject: POST /api/documents (${title}) → ${res.status()} ${await res.text()}`);
    }
    return ((await res.json()) as { document_id: string }).document_id;
  };
  return { projectId, docA: await ensureDoc(E2E_DOC_A_TITLE), docB: await ensureDoc(E2E_DOC_B_TITLE) };
}

/**
 * Doc pair with NO skip path — ALWAYS the dedicated e2e project.
 *
 * # ARCH: discovery-first is order-dependent, not deterministic. Why: the first
 * # project with >=2 documents shifts with which projects exist (the scratch
 * # project an earlier spec created, a dev stand's real projects), and docs[0]
 * # may be the INDEX doc — a document with different panel/navigation behavior
 * # than the ordinary docs these specs were written against (observed: the
 * # refs panel never loaded the index doc's scope). The dedicated project's
 * # titled, ordinary documents are the same shape on every stand, every run.
 */
export async function ensureDocPair(page: Page): Promise<DocPairScope> {
  const ensured = await ensureE2eProject(page);
  return { projectId: ensured.projectId, docA: ensured.docA, docB: ensured.docB };
}

/**
 * Chat scope with NO skip path — ALWAYS the dedicated e2e project, with a
 * doc-scoped AI chat per document (find-or-create by document, titled for
 * unambiguous row selection in the session list). Same determinism rationale
 * as ensureDocPair: the dedicated project is the same shape on every stand.
 */
export async function ensureChatScope(page: Page): Promise<ChatScope> {
  const { projectId, docA, docB } = await ensureE2eProject(page);
  const ensureSession = async (docId: string, title: string): Promise<string> => {
    const sessions = (await (
      await page.request.get(`/api/chat/sessions?project_id=${projectId}&document_id=${docId}`)
    ).json()) as Array<{ session_id: string; reference_id: string | null; title?: string }>;
    const existing = sessions.find((s) => s.reference_id == null && s.title === title);
    if (existing) return existing.session_id;
    const res = await page.request.post('/api/chat/sessions', {
      data: { project_id: projectId, document_id: docId, is_note: false },
    });
    if (!res.ok()) {
      throw new Error(`ensureChatScope: POST /api/chat/sessions (${title}) → ${res.status()} ${await res.text()}`);
    }
    const sessionId = ((await res.json()) as { session_id: string }).session_id;
    const patch = await page.request.patch(`/api/chat/sessions/${sessionId}`, {
      data: { title },
    });
    if (!patch.ok()) {
      throw new Error(`ensureChatScope: PATCH title (${title}) → ${patch.status()} ${await patch.text()}`);
    }
    return sessionId;
  };
  return {
    projectId,
    docA: { docId: docA, sessionId: await ensureSession(docA, '[e2e-scope] chat A') },
    docB: { docId: docB, sessionId: await ensureSession(docB, '[e2e-scope] chat B') },
  };
}

export interface DocPairScope {
  projectId: string;
  docA: string;
  docB: string;
}



/**
 * Find the per-spec scratch doc by title and RESET it (delete + recreate) so a
 * rerun starts from a fresh Yjs state — a REST content PATCH does NOT reset the
 * live collab doc (the verdict-card drive's fixture trap: doc A's Yjs still held
 * another spec's text while the store held the seeded token, and the agent's
 * edit failed on old_string). Bounded: one doc per slug, reused across runs.
 */
export async function ensureFreshScratchDoc(
  page: Page,
  projectId: string,
  slug: string,
  content: string,
): Promise<string> {
  const title = `[e2e-scratch] ${slug}`;
  const detail = (await (await page.request.get(`/api/projects/${projectId}`)).json()) as {
    documents?: Array<{ document_id: string; title: string }>;
  };
  const existing = (detail.documents ?? []).find(d => d.title === title);
  if (existing) {
    const del = await page.request.delete(`/api/documents/${existing.document_id}`);
    if (!del.ok()) {
      throw new Error(`ensureFreshScratchDoc: DELETE ${existing.document_id} → ${del.status()}`);
    }
  }
  const res = await page.request.post('/api/documents', {
    data: { project_id: projectId, title, content },
  });
  if (!res.ok()) {
    throw new Error(`ensureFreshScratchDoc: POST (${title}) → ${res.status()}`);
  }
  return ((await res.json()) as { document_id: string }).document_id;
}

/**
 * Find or create a DEDICATED per-spec scratch document inside the dedicated e2e
 * project (`[e2e-scratch] <slug>`).
 *
 * INVARIANT: table specs MUST mutate only their scratch doc, never a user's real
 * document. Why: the table specs seed/delete table objects and dispatch anchor
 * rewrites; running them against a user's working doc corrupts real data — the
 * "Таблички" incident, where an e2 run left an orphan anchor.
 * # ARCH: ONE doc PER SPEC (not one shared doc). Why: the specs assert on
 * `.cm-table-block` rows of "the" scratch doc; a shared doc made each spec see
 * the previous spec's seeded tables and match a stale `.last()` (observed:
 * table-block's type-into-cell assertion read the deadspace spec's matrix).
 * Per-slug docs are still bounded (one per spec, reused across runs) and all
 * live in the dedicated project, where the second e2e user can be granted access.
 */
export async function ensureTableScratchDoc(
  page: Page,
  slug: string,
): Promise<{ projectId: string; docId: string }> {
  const title = `[e2e-scratch] ${slug}`;
  const ensured = await ensureE2eProject(page);
  const detail = (await (await page.request.get(`/api/projects/${ensured.projectId}`)).json()) as {
    documents?: Array<{ document_id: string; title: string }>;
  };
  const existing = detail.documents?.find((d) => d.title === title);
  if (existing) return { projectId: ensured.projectId, docId: existing.document_id };
  const res = await page.request.post('/api/documents', {
    data: { project_id: ensured.projectId, title, content: '' },
  });
  if (!res.ok()) {
    throw new Error(`ensureTableScratchDoc: POST /api/documents (${title}) → ${res.status()} ${await res.text()}`);
  }
  return {
    projectId: ensured.projectId,
    docId: ((await res.json()) as { document_id: string }).document_id,
  };
}

/**
 * Grant `access_level` on the dedicated e2e project to the second seeded e2e user
 * (black@lore.app) so multi-user specs can open it. Owner-invite by email is
 * idempotent — an existing membership is not duplicated.
 */
export async function ensureE2eSecondUser(
  page: Page,
  email = 'black@lore.app',
  accessLevel = 'full',
): Promise<void> {
  const ensured = await ensureE2eProject(page);
  const res = await page.request.post(`/api/projects/${ensured.projectId}/members`, {
    data: { email, access_level: accessLevel },
  });
  if (!res.ok()) {
    throw new Error(`ensureE2eSecondUser: POST members (${email}) → ${res.status()} ${await res.text()}`);
  }
}
