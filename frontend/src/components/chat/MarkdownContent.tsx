/**
 * Renders assistant markdown in the chat panel over dsh's sealed renderer —
 * `frontend/src/dsh/lore-markdown.js`: a real
 * CommonMark+GFM+TeX pipeline with shiki fences, KaTeX, and dsh's incremental
 * streaming parser. Raw HTML is escaped by policy, not by us.
 *
 * The Lore layer dsh cannot state, each with its named consumer:
 * - internal link schemes doc:/ref:/note: — dsh's sanitizeUrl allowlists only
 *   http(s)/mailto, so PROSE segments are pre-rewritten to the absolute
 *   https://lore.local/l/<type>/<id> form (an allowlisted destination dsh
 *   renders as a real anchor); the delegated click handler below and
 *   EditorLinkPreview's href-derived branch decode it via parseInAppLink.
 *   Fenced code keeps the literal scheme.
 * - mermaid — absent from dsh's ui-primitives; ```mermaid fence segments
 *   render through editor/mermaid-render.ts (shared with the editor).
 *
 * Code-block copy is dsh's alone (CodeBlock writes the clipboard and swaps
 * `labels.code`); a second Lore write on the same click races it and
 * Firefox rejects one of the two, so no toast layer rides that button.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { MarkdownTextProps } from '../../dsh/lore-markdown';
import { renderMermaid, getCachedMermaid } from '../../editor/mermaid-render';
import { useTranslation } from '../../i18n';
import { emit } from '../../events';
import { parseInAppLink } from '../../utils/in-app-link';

interface Props {
  content: string;
  /** True while the owning turn streams: only the LAST split segment renders
   * live (dsh's incremental parser owns the tail); settled segments freeze as
   * memoized <MarkdownText> elements. Source: the chat store's streaming
   * state threaded through MessageBubble / turn-nodes. */
  streaming?: boolean;
}

// ARCH: lazy bundle — the sealed renderer (shiki singleton warm-up is a
// 120–175 ms long task) loads on FIRST chat render via dynamic import(), so
// the landing page and the editor never pay it. Module-level promise: every
// chat mount shares one load; the css rides the same import so both arrive
// with the chunk, never with the main bundle.
type MarkdownBundle = typeof import('../../dsh/lore-markdown');
let bundlePromise: Promise<MarkdownBundle> | null = null;
function loadMarkdownBundle(): Promise<MarkdownBundle> {
  if (!bundlePromise) {
    bundlePromise = Promise.all([
      import('../../dsh/lore-markdown'),
      import('../../dsh/lore-markdown.css'),
    ]).then(([mod]) => mod);
  }
  return bundlePromise;
}

// ─── fence split ────────────────────────────────────────────────────────
// ARCH: the source is split on TOP-LEVEL fences (column-0 ```/~~~ markers; a
// fence indented inside a list stays part of its prose segment, so the split
// never cuts a list or a table). Each prose / non-mermaid fence segment
// renders as its own <MarkdownText>; only the LAST segment is live while
// streaming, so per-chunk work tracks the tail — the same shape as dsh's
// incremental parser, one fence earlier.

type Segment =
  | { kind: 'prose'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'mermaid'; code: string };

const FENCE_OPEN_RE = /^ {0,3}(`{3,}|~{3,})\s*(.*)$/;
const FENCE_CLOSE_RE = /^ {0,3}(`{3,}|~{3,})\s*$/;

function splitSegments(src: string): Segment[] {
  const lines = src.split('\n');
  const segments: Segment[] = [];
  let prose: string[] = [];
  const flushProse = () => {
    if (prose.length > 0) {
      segments.push({ kind: 'prose', text: prose.join('\n') });
      prose = [];
    }
  };
  let i = 0;
  while (i < lines.length) {
    const open = lines[i].match(FENCE_OPEN_RE);
    if (!open) {
      prose.push(lines[i]);
      i += 1;
      continue;
    }
    const marker = open[1];
    const info = open[2].trim().split(/\s+/)[0] ?? '';
    const start = i;
    i += 1;
    // bodyEnd: index of the closing marker line, or lines.length while the
    // fence is still open (streaming tail) — the mermaid body must stop BEFORE
    // it: a trailing ``` inside the diagram source is a stray node in a graph
    // and a parse error in a sequenceDiagram.
    let bodyEnd = lines.length;
    while (i < lines.length) {
      const close = lines[i].match(FENCE_CLOSE_RE);
      if (close && close[1][0] === marker[0] && close[1].length >= marker.length) {
        bodyEnd = i;
        i += 1;
        break;
      }
      i += 1;
    }
    flushProse();
    if (info === 'mermaid') {
      segments.push({ kind: 'mermaid', code: lines.slice(start + 1, bodyEnd).join('\n') });
    } else {
      // Fence verbatim (markers + body, closed or streaming-open) — dsh
      // renders it as its own CodeBlock.
      segments.push({ kind: 'code', text: lines.slice(start, i).join('\n') });
    }
  }
  flushProse();
  return segments;
}

// Internal links in PROSE segments only: `[alt](doc:ID)` → the lore.local
// form. The optional leading `!` keeps IMAGES unwritten (an internal id must
// not become an <img> that fetches lore.local — dsh unwraps it to alt text);
// alt without nested brackets covers the internal-link shapes we emit. Fenced
// code is never touched by construction — the rewrite runs on prose segment
// text alone. Accepted, recorded: the literal inside inline code and
// 4-space-indented code IS rewritten too (visible as the lore.local form
// there).
const INTERNAL_LINK_RE = /(!?)\[([^[\]]*)\]\((doc|ref|note):([^)\s]+)\)/g;
function rewriteInternalLinks(text: string): string {
  return text.replace(INTERNAL_LINK_RE, (match, image: string, alt: string, type: string, id: string) =>
    image ? match : `[${alt}](https://lore.local/l/${type}/${id})`);
}

// ─── click delegation ───────────────────────────────────────────────────

function handleInAppLinkClick(e: React.MouseEvent<HTMLDivElement>) {
  const anchor = (e.target as HTMLElement).closest('a');
  if (!anchor) return;
  const href = anchor.getAttribute('href') || '';
  const parsed = parseInAppLink(href);
  if (!parsed) return;
  e.preventDefault();
  e.stopPropagation();
  const { type, id } = parsed;
  // INVARIANT: a ref link in the answer and the same ref in the sources list behave
  // identically — stayInContext keeps the current chat scope (no navigate to the ref's
  // parent doc). Why: both point at the same object; without stayInContext the handler
  // navigates to the parent doc and the per-document chat scope switches (long-standing
  // bug). Mirrors Sources.handleRefClick.
  if (type === 'ref') emit('navigate-to-reference', { referenceId: id, stayInContext: true });
  else if (type === 'doc') emit('navigate-to-document', { documentId: id });
  else if (type === 'note') emit('open-notes', { threadId: id });
}

// ─── wrapper ────────────────────────────────────────────────────────────

export function MarkdownContent({ content, streaming = false }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const { t } = useTranslation();
  const [bundle, setBundle] = useState<MarkdownBundle | null>(null);

  useEffect(() => {
    let alive = true;
    loadMarkdownBundle().then(mod => { if (alive) setBundle(mod); });
    return () => { alive = false; };
  }, []);

  // Stable per locale (t identity changes on language switch): a fresh object
  // per render would rebuild MarkdownText's streaming cache mid-message —
  // same discipline as dsh's own consumer (AssistantMarkdown).
  const labels = useMemo<MarkdownTextProps['labels']>(
    () => ({ code: { copyLabel: t('copy'), copiedLabel: t('copied') }, footnotes: t('footnotes') }),
    [t],
  );

  const segments = useMemo(() => splitSegments(content), [content]);
  const last = segments.length - 1;

  // Mermaid diagrams — rendered per segment code through the shared
  // editor/mermaid-render (cached + serialized there); SVGs live in React
  // state and land via dangerouslySetInnerHTML on this wrapper-owned node,
  // never inside dsh's React subtree. Resolutions are keyed by code, so
  // streaming edits to a still-open fence re-render only the new code.
  const [mermaidSvgs, setMermaidSvgs] = useState<Record<string, string>>({});
  useEffect(() => {
    let alive = true;
    for (const seg of segments) {
      if (seg.kind !== 'mermaid') continue;
      const known = mermaidSvgs[seg.code] ?? getCachedMermaid(seg.code);
      if (known !== undefined) {
        setMermaidSvgs(prev => (prev[seg.code] === known ? prev : { ...prev, [seg.code]: known }));
        continue;
      }
      renderMermaid(seg.code).then(svg => {
        if (alive) setMermaidSvgs(prev => (prev[seg.code] === svg ? prev : { ...prev, [seg.code]: svg }));
      });
    }
    return () => { alive = false; };
    // mermaidSvgs participates: a value read from the shared cache must be
    // able to land in state; resolved codes settle (guard returns prev), and
    // renderMermaid dedupes in-flight work per code.
  }, [segments, mermaidSvgs]);

  // Bundle still loading: the raw source stays readable (no silent
  // degradation) until the renderer swaps in — one paint on first chat render.
  if (!bundle) {
    return (
      <div ref={ref} className="chat-markdown">
        <div className="chat-markdown-pending whitespace-pre-wrap">{content}</div>
      </div>
    );
  }

  const MarkdownText = bundle.MarkdownText;
  return (
    <div ref={ref} className="chat-markdown" onClick={handleInAppLinkClick}>
      {segments.map((seg, i) => {
        const liveTail = streaming && i === last;
        if (seg.kind === 'mermaid') {
          // While the fence is still OPEN (live tail), show the plain source —
          // the diagram renders once the fence closes (validation item).
          if (liveTail) return <pre key={i}>{seg.code}</pre>;
          const svg = mermaidSvgs[seg.code];
          return <div key={i} className="chat-mermaid" dangerouslySetInnerHTML={{ __html: svg ?? '' }} />;
        }
        const text = seg.kind === 'prose' ? rewriteInternalLinks(seg.text) : seg.text;
        return <MarkdownText key={i} text={text} streaming={liveTail} labels={labels} />;
      })}
    </div>
  );
}
