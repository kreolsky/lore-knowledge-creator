/**
 * The driver-owned turn e2e.
 *
 * A chat session runs its
 * turn over the project lifecycle WS (SYSTEM: chat-fanout): the completions
 * POST answers JSON (never an SSE stream), the frames arrive as
 * `{type:'chat_frame', session_id, frame}` envelopes, and the browser's ONE
 * sink renders them. Pinned here, on the existing helpers (scope/prefs/net):
 *  1. the transport witness: the completions response's content-type is JSON;
 *  2. the WS witness: chat_frame envelopes for the session arrive IN ORDER
 *     (ids first) and end with done + turn_closed;
 *  3. the render witness: the reply text lands in the panel;
 *  4. the row witness: the assistant row carries content + driver_seq after a
 *     reload read (SSE-turn parity: shape-identical rows).
 *
 * Real LLM turn — gated on E2E_AGENT_TURN like the agent-surface drives.
 */
import { test, expect } from '@playwright/test';
import { ensureE2eProject, ensureFreshScratchDoc } from './helpers/scope';
import { seedChatTabOpen, seedLastActiveChat } from './helpers/prefs';
import { NetRecorder } from './helpers/net';
import { chatActivatedInPhase } from './helpers/chat';

const SLUG = 'harness-lifecycle';
const MARKER = 'PARITYOK7';

test.setTimeout(300_000);

test('a session turns over the project WS — JSON completions, chat_frame envelopes, parity rows', async ({ page }) => {
  test.skip(!process.env.E2E_AGENT_TURN, 'real LLM turn — set E2E_AGENT_TURN=1 to drive it');
  const rec = new NetRecorder(page);
  const { projectId } = await ensureE2eProject(page);
  const docId = await ensureFreshScratchDoc(page, projectId, SLUG, 'harness lifecycle e2e body');

  // A dedicated session on the scratch doc (harness by default).
  const res = await page.request.post('/api/chat/sessions', {
    data: { project_id: projectId, document_id: docId, is_note: false },
  });
  expect(res.ok()).toBeTruthy();
  const { session_id: sessionId } = (await res.json()) as { session_id: string };

  // Deterministic surface: the chat tab open on the doc + the session
  // restored as the active chat.
  await seedChatTabOpen(page, projectId, [docId]);
  await seedLastActiveChat(page, projectId, sessionId);

  // The WS witness: every chat_frame envelope for THIS session, in order.
  const frameTypes: string[] = [];
  page.on('websocket', ws => {
    ws.on('framereceived', f => {
      try {
        const msg = JSON.parse(String(f.payload)) as { type?: string; session_id?: string; frame?: { type?: string } };
        if (msg.type === 'chat_frame' && msg.session_id === sessionId && msg.frame?.type) {
          frameTypes.push(msg.frame.type);
        }
      } catch { /* non-JSON control frames */ }
    });
  });
  // The transport witness: the completions POST must answer JSON, not SSE.
  const completionsTypes: string[] = [];
  page.on('response', resp => {
    if (resp.url().includes(`/api/chat/sessions/${sessionId}/completions`)
      && resp.request().method() === 'POST') {
      completionsTypes.push(resp.headers()['content-type'] ?? '');
    }
  });

  rec.phase('open');
  await page.goto(`/docs/${docId}`);
  await expect
    .poll(() => chatActivatedInPhase(rec, 'open') === sessionId, { timeout: 20_000 })
    .toBe(true);

  const textarea = page.locator('.right-panel textarea').first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(`Reply with exactly the word ${MARKER} and nothing else.`);
  await textarea.press('ControlOrMeta+Enter');

  // The WS frames end the turn: ids → … → done → turn_closed.
  await expect
    .poll(() => frameTypes.includes('turn_closed'), { timeout: 180_000 })
    .toBe(true);
  expect(frameTypes[0]).toBe('ids');
  expect(frameTypes).toContain('done');
  expect(frameTypes.indexOf('turn_closed')).toBeGreaterThan(frameTypes.indexOf('done'));
  // The transport witness.
  expect(completionsTypes.length).toBeGreaterThan(0);
  for (const ct of completionsTypes) expect(ct).toContain('application/json');

  // The render witness: the reply text landed in the panel.
  await expect(page.locator('.right-panel')).toContainText(MARKER, { timeout: 15_000 });

  // The row witness: SSE-turn parity — content + driver_seq on the assistant row.
  const msgs = (await (
    await page.request.get(`/api/chat/sessions/${sessionId}/messages`)
  ).json()) as Array<{ role: string; content?: string; driver_seq?: number }>;
  const assistant = msgs.find(m => m.role === 'assistant');
  expect(assistant?.content).toContain(MARKER);
  expect(assistant?.driver_seq).toBeTruthy();
});
