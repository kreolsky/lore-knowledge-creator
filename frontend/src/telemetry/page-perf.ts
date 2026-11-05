/**
 * Page-level perf producers for the telemetry channel: page load, document switch,
 * input latency, attributed freezes.
 *
 * see SYSTEM: telemetry. Rows:
 *   load             one per page load, sent once LCP has settled or the tab hides; carries
 *                    ws_ms (first collab socket, connect to open) and editable_ms (page
 *                    start to the editor accepting input, absent for a read-only user)
 *   doc-switch       a tree or link open until the new document's text is painted
 *   interaction      windowed aggregate of click and keydown latency
 *   slow-interaction raw row past LONGTASK_MS
 *   lag-frame        a Long Animation Frame past LONGTASK_MS, with the scripts that ran
 *
 * ARCH: this module is in the entry bundle and takes the editor view as an opaque object,
 * so it must not import anything that reaches CodeMirror.
 */

import { sendTelemetry, flushBeacon } from './telemetry';
import { LONGTASK_MS, RollingAggregate, isPlausiblePerfMs } from './perf';

// Long enough for LCP to settle on a slow link, short enough to report a tab left open.
const LOAD_REPORT_DELAY_MS = 30_000;
// An open that never reveals (same-document click, failed load) must not match a later reveal.
const DOC_OPEN_TIMEOUT_MS = 30_000;
// The Event Timing API's minimum threshold.
const EVENT_TIMING_MIN_MS = 16;
const TOP_SCRIPTS = 3;

/** The kind of page at a path. No ids: the row's entity_id carries those where they matter. */
function surfaceOf(pathname: string): string {
  if (pathname === '/') return 'landing';
  if (pathname.startsWith('/docs/')) return 'document';
  if (pathname === '/projects') return 'dashboard';
  if (pathname.startsWith('/projects/')) return 'project';
  return 'other';
}

const currentSurface = (): string => surfaceOf(location.pathname);
// WHY: /api/telemetry needs a session, so a row from the landing page ends in a 401.
const onLanding = (): boolean => location.pathname === '/';
const domNodes = (): number => document.getElementsByTagName('*').length;
const kb = (bytes: number): number => Math.round(bytes / 1024);
const supports = (type: string): boolean => PerformanceObserver.supportedEntryTypes?.includes(type) ?? false;

function observe(type: string, init: PerformanceObserverInit, onEntry: (e: PerformanceEntry) => void): void {
  if (!supports(type)) return;
  new PerformanceObserver(list => list.getEntries().forEach(onEntry)).observe({ type, buffered: true, ...init });
}

// ─── Page load ──────────────────────────────────────────────────────────────

interface LoadSample {
  path: string;
  fcp?: number;
  lcp?: number;
  textMs?: number;
  wsMs?: number;
  editableMs?: number;
  docKb?: number;
  entityId?: string;
}

let load: LoadSample | null = null;

function reportLoad(): void {
  if (!load) return;
  const sample = load;
  load = null;
  const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
  const js = (performance.getEntriesByType('resource') as PerformanceResourceTiming[])
    .filter(r => new URL(r.name).pathname.endsWith('.js'));
  sendTelemetry({
    category: 'perf', kind: 'load', entity_id: sample.entityId,
    detail: {
      surface: surfaceOf(sample.path),
      nav: nav?.type,
      ttfb: nav ? Math.round(nav.responseStart) : undefined,
      fcp: sample.fcp,
      lcp: sample.lcp,
      text_ms: sample.textMs,
      ws_ms: sample.wsMs,
      editable_ms: sample.editableMs,
      doc_kb: sample.docKb,
      js_files: js.length,
      js_transfer_kb: kb(js.reduce((sum, r) => sum + r.transferSize, 0)),
      js_decoded_kb: kb(js.reduce((sum, r) => sum + r.decodedBodySize, 0)),
      dom_nodes: domNodes(),
    },
  });
}

function startLoadSample(): void {
  // WHY: a page that started hidden paints late for a reason that has nothing to do with it.
  if (onLanding() || document.visibilityState === 'hidden') return;
  const sample: LoadSample = { path: location.pathname };
  load = sample;
  observe('paint', {}, e => { if (e.name === 'first-contentful-paint') sample.fcp = Math.round(e.startTime); });
  observe('largest-contentful-paint', {}, e => { sample.lcp = Math.round(e.startTime); });
  setTimeout(reportLoad, LOAD_REPORT_DELAY_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'hidden' || !load) return;
    reportLoad();
    // WHY: telemetry.ts binds its unload listener on the first event, which may be the
    // one just sent, and a listener added during this dispatch does not run.
    flushBeacon();
  });
}

// ─── Document switch ────────────────────────────────────────────────────────

let pendingOpen: { docId: string; start: number } | null = null;
const shownViews = new WeakSet<object>();

/** A document open began: the clock a following markContentShown stops. */
export function markDocOpenStart(docId: string): void {
  pendingOpen = { docId, start: performance.now() };
}

/** Run after the next paint: rAF fires before it, and a macrotask from there lands after. */
function afterNextPaint(fn: () => void): void {
  requestAnimationFrame(() => setTimeout(fn, 0));
}

/**
 * The editor revealed an entity's text. Stops the running document open, or fills in the
 * page load's text time when the page was opened on this entity. One report per view,
 * because the reveal runs twice: on ready and from the safety timer.
 */
export function markContentShown(view: object, entityId: string, chars: number): void {
  if (shownViews.has(view)) return;
  shownViews.add(view);
  afterNextPaint(() => {
    const now = performance.now();
    const docKb = kb(chars);
    const open = pendingOpen;
    // WHY only on a match: a late reveal of the previous document must not cancel the open
    // that replaced it, or rapid switching drops out of the data.
    if (open?.docId === entityId) {
      pendingOpen = null;
      if (now - open.start < DOC_OPEN_TIMEOUT_MS) {
        sendTelemetry({
          category: 'perf', kind: 'doc-switch', entity_id: entityId,
          detail: { ms: Math.round(now - open.start), doc_kb: docKb, dom_nodes: domNodes() },
        });
        return;
      }
    }
    if (load && load.textMs === undefined && load.path === location.pathname) {
      load.textMs = Math.round(now);
      load.docKb = docKb;
      load.entityId = entityId;
    }
  });
}

/** A sample still describing the page on screen, not a load the user navigated away from. */
function loadOfThisPage(): LoadSample | null {
  return load && load.path === location.pathname ? load : null;
}

/** The page's first collab socket opened, `ms` after its first connect attempt. */
export function markCollabSocketOpen(ms: number): void {
  const sample = loadOfThisPage();
  if (sample && sample.wsMs === undefined) sample.wsMs = Math.round(ms);
}

/** The editor accepts input for the first time since the page started. */
export function markEditable(): void {
  const sample = loadOfThisPage();
  if (sample && sample.editableMs === undefined) sample.editableMs = Math.round(performance.now());
}

// ─── Input latency ──────────────────────────────────────────────────────────

const interactionAggregate = new RollingAggregate('interaction');

function startInteractionObserver(): void {
  observe('event', { durationThreshold: EVENT_TIMING_MIN_MS } as PerformanceObserverInit, e => {
    const entry = e as PerformanceEventTiming;
    // One entry per user action: a click also yields pointer entries, a key its keyup.
    if (onLanding() || !entry.interactionId || (entry.name !== 'click' && entry.name !== 'keydown')) return;
    if (!isPlausiblePerfMs(entry.duration)) return;
    interactionAggregate.add(entry.duration);
    if (entry.duration < LONGTASK_MS) return;
    sendTelemetry({
      category: 'perf', kind: 'slow-interaction',
      detail: { ms: entry.duration, name: entry.name, surface: currentSurface(), dom_nodes: domNodes() },
    });
  });
}

// ─── Attributed freezes ─────────────────────────────────────────────────────

interface LoafScript {
  duration: number;
  invokerType: string;
  sourceURL: string;
  sourceFunctionName: string;
}

function startLongFrameObserver(): void {
  observe('long-animation-frame', {}, e => {
    if (onLanding() || e.duration <= LONGTASK_MS || !isPlausiblePerfMs(e.duration)) return;
    const { blockingDuration, scripts } = e as PerformanceEntry & { blockingDuration: number; scripts: LoafScript[] };
    sendTelemetry({
      category: 'perf', kind: 'lag-frame',
      detail: {
        ms: Math.round(e.duration),
        blocking_ms: Math.round(blockingDuration),
        surface: currentSurface(),
        dom_nodes: domNodes(),
        scripts: [...scripts].sort((a, b) => b.duration - a.duration).slice(0, TOP_SCRIPTS).map(s => ({
          ms: Math.round(s.duration),
          invoker: s.invokerType,
          fn: s.sourceFunctionName,
          file: s.sourceURL.split('/').pop(),
        })),
      },
    });
  });
}

/** Start every page-level producer. Call once, before the first render. */
export function startPagePerf(): void {
  if (typeof PerformanceObserver === 'undefined') return;
  try {
    startLoadSample();
    startInteractionObserver();
    startLongFrameObserver();
  } catch (e) {
    // WHY: log only, like every telemetry path: diagnostics must never break the page.
    console.warn('page perf telemetry did not start', e);
  }
}
