/**
 * Mermaid diagram rendering with caching and dark-mode support.
 *
 * ARCH: Separate module to isolate mermaid dependency from CM6 plugin logic,
 * mirroring the pattern in math-render.ts for KaTeX.
 */
// SYSTEM: mermaid-render — Mermaid diagram rendering for CM6 + chat (cached, dark mode aware)

import { escapeHtml } from '../utils/html';
import { createBoundedMemo } from '../utils/bounded-memo';

// ARCH: mermaid is dynamically
// imported on first render so it (and its parser deps) moves to its own chunk
// off the main bundle. ensureInit + the renderChain serialize the singleton,
// non-reentrant parser, so the lazy load resolves exactly once.
let mermaidMod: typeof import('mermaid').default | null = null;
async function loadMermaid(): Promise<typeof import('mermaid').default> {
  if (mermaidMod === null) mermaidMod = (await import('mermaid')).default;
  return mermaidMod;
}

let initialized = false;
let idCounter = 0;

function isDark(): boolean {
  return document.documentElement.classList.contains('dark');
}

async function ensureInit(): Promise<void> {
  if (initialized) return;
  initialized = true;
  const mermaid = await loadMermaid();
  mermaid.initialize({
    startOnLoad: false,
    theme: isDark() ? 'dark' : 'default',
    securityLevel: 'loose',
  });
}

// WHY bounded LRU (was: clear-at-threshold at 200): crossing the bound mid-chat
// used to wipe every rendered diagram, forcing a re-render of the visible set —
// and mermaid.render is 10-100× a katex render. Policy and measurement live in
// utils/bounded-memo.ts (plan resource-cache-one-primitive, step 6). The
// in-flight map + renderChain below are NOT part of the memo and stay as-is.
const cache = createBoundedMemo<string>(200);

// In-flight renders deduped by code, and a serialization chain.
const inflight = new Map<string, Promise<string>>();
let renderChain: Promise<unknown> = Promise.resolve();

/** Synchronous cache lookup — lets callers paint an already-rendered diagram before the next frame, with no async gap. */
export function getCachedMermaid(code: string): string | undefined {
  return cache.get(code);
}

async function renderUncached(code: string): Promise<string> {
  await ensureInit();
  const mermaid = await loadMermaid();
  try {
    const id = `mermaid-svg-${idCounter++}`;
    const { svg } = await mermaid.render(id, code);
    cache.set(code, svg);
    return svg;
  } catch {
    const errorHtml = `<div class="cm-mermaid-error">${escapeHtml(code)}</div>`;
    cache.set(code, errorHtml);
    return errorHtml;
  }
}

// INVARIANT: mermaid.render runs strictly one-at-a-time, deduped per code.
// Why: mermaid's parser uses singleton module state and is NOT reentrant; the chat
// fires many renders concurrently while streaming (measured 7 in flight), which
// wastes work and risks cross-diagram corruption. Serializing + dedup makes each
// unique diagram render exactly once, in order.
export function renderMermaid(code: string): Promise<string> {
  const cached = cache.get(code);
  if (cached !== undefined) return Promise.resolve(cached);
  const existing = inflight.get(code);
  if (existing) return existing;
  const run = renderChain.then(() => renderUncached(code));
  inflight.set(code, run);
  renderChain = run.catch(() => {});
  void run.finally(() => { if (inflight.get(code) === run) inflight.delete(code); });
  return run;
}
