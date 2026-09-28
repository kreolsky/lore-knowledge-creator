/**
 * Chat-tab e2e helpers shared across the chat specs.
 *
 * WHY recorder-polling instead of "wait for a NEW request after click": the AI
 * chat tab is frequently the already-active default (seeded via helpers/prefs.ts
 * or restored from prefs), so clicking fires no new /chat/sessions request and a
 * `waitForRequest` after the click times out. Poll the recorder for a load that
 * may already have landed; only click when none has.
 */
import { expect, type Page } from '@playwright/test';
import type { NetRecorder } from './net';

const SESSIONS_PATH = '/api/chat/sessions';

/** True once an AI (non-note) GET /chat/sessions scoped to `documentId` was seen. */
export function aiSessionsLoaded(rec: NetRecorder, documentId: string): boolean {
  return rec.records.some(
    (r) =>
      r.method === 'GET' &&
      r.path === SESSIONS_PATH &&
      r.query.get('is_note') !== 'true' &&
      r.query.get('document_id') === documentId,
  );
}

/** Make the AI chat tab active for `documentId` and wait until its sessions load. */
export async function ensureChatLoaded(
  page: Page,
  documentId: string,
  rec: NetRecorder,
): Promise<void> {
  if (!aiSessionsLoaded(rec, documentId)) {
    // exact: the composer's model picker ("local/…/chat Vision-…") also
    // contains "Chat" — a non-exact name resolves to 2 buttons and dies in
    // strict mode the moment the composer renders first.
    await page.getByRole('button', { name: 'Chat', exact: true }).click({ force: true });
  }
  await expect
    .poll(() => aiSessionsLoaded(rec, documentId), {
      timeout: 15_000,
      message: `no AI /chat/sessions load for ${documentId}`,
    })
    .toBe(true);
}

const MESSAGES_LOAD_RE = /^\/api\/chat\/sessions\/([^/]+)\/messages$/;

/**
 * Session id of the first GET /chat/sessions/{id}/messages recorded in `phase`
 * (the ACTIVATED session — restore_saved and list-click both land here), or null.
 */
export function messagesLoadInPhase(rec: NetRecorder, phase: string): string | null {
  for (const r of rec.inPhase(phase)) {
    if (r.method !== 'GET') continue;
    const m = r.path.match(MESSAGES_LOAD_RE);
    if (m) return m[1];
  }
  return null;
}

/**
 * The session the chat panel ACTIVATED in `phase`, or null. The restore path
 * piggybacks the active session's messages on the sessions-load response (no
 * separate /messages GET), so the piggyback body is consulted first and the
 * fallback messages GET second — together they witness either activation path.
 */
export function chatActivatedInPhase(rec: NetRecorder, phase: string): string | null {
  const piggyback = rec.chatActivations.find((a) => a.phase === phase);
  if (piggyback) return piggyback.sessionId;
  return messagesLoadInPhase(rec, phase);
}
