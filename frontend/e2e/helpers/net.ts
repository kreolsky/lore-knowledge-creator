/**
 * Phase-marked request recorder + matchers.
 *
 * ARCH: the runtime signal this harness exists for is the NETWORK, not the DOM
 * (thin client — scope/dedup/hydration are all visible in requests, and the app
 * has only ~6 data-testids). A spec marks logical phases (`rec.phase('open-doc')`),
 * then asserts which requests fired in each phase and with what scope.
 *
 * Subsystem disambiguation: GET /api/chat/sessions serves two subsystems on the
 * same path, told apart by the `is_note` query param — present ⇒ NOTE chats,
 * absent ⇒ AI chats (see store/note-chat-store.ts vs chat-store/sessions-slice.ts).
 */
import { expect, type Page, type Request } from '@playwright/test';

export interface NetRecord {
  phase: string;
  method: string;
  url: string;
  path: string;
  query: URLSearchParams;
}

export type Subsystem = 'note' | 'ai';

export class NetRecorder {
  readonly records: NetRecord[] = [];
  /**
   * Chat activations observed via the sessions-load piggyback: one entry per
   * GET /api/chat/sessions?with_active_messages=true response whose body carried
   * active_messages (the restore path commits those directly, WITHOUT a separate
   * /messages GET — so the piggyback body is the only network witness of the
   * activation; see load-actions.ts restore_saved).
   */
  readonly chatActivations: Array<{ phase: string; sessionId: string }> = [];
  private current = 'init';

  constructor(page: Page) {
    page.on('request', (req: Request) => this.record(req));
    page.on('response', (resp) => {
      const url = resp.url();
      if (
        resp.request().method() === 'GET' &&
        url.includes('/api/chat/sessions') &&
        url.includes('with_active_messages=true')
      ) {
        void resp
          .json()
          .then((body: { active_messages?: { session_id?: string } | null }) => {
            const sid = body?.active_messages?.session_id;
            if (sid) this.chatActivations.push({ phase: this.current, sessionId: sid });
          })
          .catch(() => undefined);
      }
    });
  }

  private record(req: Request): void {
    const url = req.url();
    let path = url;
    let query = new URLSearchParams();
    try {
      const u = new URL(url);
      path = u.pathname;
      query = u.searchParams;
    } catch {
      /* non-URL (data:) requests — keep raw url as path */
    }
    this.records.push({ phase: this.current, method: req.method(), url, path, query });
  }

  /** Mark the start of a logical phase; subsequent requests are tagged with it. */
  phase(name: string): void {
    this.current = name;
  }

  inPhase(name: string): NetRecord[] {
    return this.records.filter((r) => r.phase === name);
  }
}

const SESSIONS_PATH = '/api/chat/sessions';

function subsystemOf(rec: NetRecord): Subsystem {
  return rec.query.get('is_note') === 'true' ? 'note' : 'ai';
}

/**
 * Assert that GET /api/chat/sessions fired in `phase` for `subsystem`, and let
 * the caller pin the document scope: `expectLoadSessions(rec, phase).scope(docId)`.
 */
export function expectLoadSessions(rec: NetRecorder, phase: string, subsystem: Subsystem = 'ai') {
  const matches = rec
    .inPhase(phase)
    .filter((r) => r.method === 'GET' && r.path === SESSIONS_PATH && subsystemOf(r) === subsystem);
  return {
    matches,
    /** Exactly `n` session loads for this subsystem fired in the phase. */
    count(n: number) {
      expect(
        matches.length,
        `expected ${n} ${subsystem} /chat/sessions load(s) in phase "${phase}", got ${matches.length}`,
      ).toBe(n);
      return this;
    },
    /** At least one load fired and every load targeted `documentId`. */
    scope(documentId: string) {
      expect(matches.length, `no ${subsystem} /chat/sessions load in phase "${phase}"`).toBeGreaterThan(0);
      for (const m of matches) {
        expect(m.query.get('document_id'), `wrong scope in phase "${phase}"`).toBe(documentId);
      }
      return this;
    },
  };
}

/**
 * Generic phase matcher for requests whose URL matches `re`.
 * `expectFetch(rec, phase, re).count(n)` — exactly `n` matching requests fired.
 */
export function expectFetch(rec: NetRecorder, phase: string, re: RegExp) {
  const matches = rec.inPhase(phase).filter((r) => re.test(r.url));
  return {
    matches,
    count(n: number) {
      expect(
        matches.length,
        `expected ${n} request(s) matching ${re} in phase "${phase}", got ${matches.length}`,
      ).toBe(n);
      return this;
    },
  };
}

/** Assert NO request whose path matches `re` fired in `phase`. */
export function expectNoRequest(rec: NetRecorder, phase: string, re: RegExp): void {
  const hit = rec.inPhase(phase).find((r) => re.test(r.path));
  expect(hit, `unexpected request in phase "${phase}": ${hit?.url}`).toBeUndefined();
}
