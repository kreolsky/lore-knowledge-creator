/**
 * Cursor navigation refinements for paragraphs and bullet/numbered list items.
 *
 * ARCH: Navigation overrides:
 * 1. Cmd+ArrowRight — visual-line-end → physical-line-end (line.to).
 *    First press: end of current visual (wrapped) line, trimmed of trailing
 *    whitespace before the wrap point. Second press (already at visual end):
 *    end of physical line / paragraph. Also stops AFTER any in-line
 *    delimiter char (sentence-ending punctuation) along the way — see
 *    NAV_DELIMITERS. Spaces are NOT stops.
 * 2. Cmd+ArrowLeft — visual-line-start → [list contentStart | heading
 *    contentStart] → line.from. Paragraphs: 2 steps. List items and headings:
 *    3 steps (intermediate stop after the bullet marker / "# " prefix). Also
 *    stops BEFORE any in-line delimiter char along the way. Moving left lands
 *    on the left edge of the delimiter (offset i); moving right lands on the
 *    right edge (offset i+1). Delimiter stops collapse/dedup with line-level
 *    stops via the shared waypoint Set.
 * 3. Cmd+Shift+ArrowLeft/Right — same staged stepping, but extends selection
 *    by moving `head` while keeping `anchor`.
 * 4. ArrowDown / ArrowUp — snap cursor to content-start when vertical
 *    navigation lands inside the ListMark area, preventing BulletWidget
 *    teardown and visual flicker.
 * 5. ArrowLeft — only explicit left from content-start moves cursor into
 *    the marker area, triggering formatting breakdown.
 *
 * INVARIANT: Vertical navigation (ArrowDown/ArrowUp) must NEVER place the
 * cursor inside the ListMark range — it always snaps to contentStart.
 * ArrowLeft from contentStart is the sole gateway into the marker zone.  Why: vertical nav snaps to contentStart (never inside the ListMark); ArrowLeft from contentStart is the only way into the marker zone, so the marker stays nav-safe.
 * Cmd-arrow handlers must move strictly monotonically (left or right) on
 * each press; never overshoot past line.from / line.to.
 */
import { syntaxTree } from '@codemirror/language';
import { EditorView, keymap } from '@codemirror/view';
import { EditorSelection, Prec, type EditorState, type Extension } from '@codemirror/state';

const LIST_MARKER_RE = /^(\s*(?:[-*]|\d+\.)\s+(?:\[[ xX]\]\s+)?)/;

// WHY: These are literal-text navigation stops (not markdown-aware) added so
// Cmd+arrow also halts at sentence-ending punctuation. Spaces, quotes, and
// brackets are intentionally excluded. A delimiter char only fires as a stop
// when at least one immediate neighbor is whitespace (line start/end count as
// whitespace). This prevents stops inside tokens like "1.5", "file.txt", "example.com",
// while preserving valid stops at sentence boundaries.
const NAV_DELIMITERS = new Set([
  '.', '!', '?',
]);


// WHY: A delimiter char at line-relative index k qualifies as a waypoint
// iff at least one immediate neighbor within the line is whitespace (/\s/).
// Line start/end are treated as whitespace. This prevents spurious stops inside
// tokens like "1.5", "file.txt", "example.com", while preserving valid stops at
// sentence boundaries ("Hello world.").
function isDelimiterAtBoundary(text: string, k: number): boolean {
  const left = k > 0 ? text[k - 1] : ' ';
  const right = k < text.length - 1 ? text[k + 1] : ' ';
  return /\s/.test(left) || /\s/.test(right);
}
// Scan the current line for delimiter chars and return waypoint offsets.
// Line-scoped (never crosses the paragraph boundary) to preserve the monotonic,
// never-overshoot invariant. `scanFrom` is an absolute doc offset; chars before
// it are skipped so list/heading MARKER characters (e.g. the "." in "1.") are
// not treated as content delimiters — existing marker-region behavior stays
// unchanged.
// Left direction: stop BEFORE the char (offset line.from + k) — the cursor
// steps past the char and lands on its left edge.
function delimiterWaypointsLeft(line: { from: number; text: string }, scanFrom: number): number[] {
  const out: number[] = [];
  for (let k = 0; k < line.text.length; k++) {
    if (line.from + k < scanFrom) continue;
    if (NAV_DELIMITERS.has(line.text[k]) && isDelimiterAtBoundary(line.text, k)) out.push(line.from + k);
  }
  return out;
}

// Right direction: stop AFTER the char (offset line.from + k + 1) — the mirror,
// the cursor steps past the char and lands on its right edge.
function delimiterWaypointsRight(line: { from: number; text: string }, scanFrom: number): number[] {
  const out: number[] = [];
  for (let k = 0; k < line.text.length; k++) {
    if (line.from + k < scanFrom) continue;
    if (NAV_DELIMITERS.has(line.text[k]) && isDelimiterAtBoundary(line.text, k)) out.push(line.from + k + 1);
  }
  return out;
}

function findListItemAt(state: EditorView['state'], pos: number): number | null {
  let listItemFrom: number | null = null;
  syntaxTree(state).iterate({
    enter(node) {
      if (node.name === 'ListItem' && node.from <= pos && node.to >= pos) {
        listItemFrom = node.from;
      }
    },
  });
  return listItemFrom;
}

function findContentStart(state: EditorView['state'], lineFrom: number): number | null {
  const line = state.doc.lineAt(lineFrom);
  const match = line.text.match(LIST_MARKER_RE);
  if (!match) return null;
  return line.from + match[1].length;
}

const HEADING_MARKER_RE = /^(\s*#{1,6}\s+)/;

function findHeadingAt(state: EditorView['state'], pos: number): number | null {
  let headingFrom: number | null = null;
  syntaxTree(state).iterate({
    enter(node) {
      if (node.name.startsWith('ATXHeading') && node.from <= pos && node.to >= pos) {
        headingFrom = node.from;
      }
    },
  });
  return headingFrom;
}

function findHeadingContentStart(state: EditorView['state'], lineFrom: number): number | null {
  const line = state.doc.lineAt(lineFrom);
  const match = line.text.match(HEADING_MARKER_RE);
  if (!match) return null;
  return line.from + match[1].length;
}

function visualLineEnd(view: EditorView, pos: number): number {
  return view.moveToLineBoundary(EditorSelection.cursor(pos), true).head;
}

function visualLineStart(view: EditorView, pos: number): number {
  return view.moveToLineBoundary(EditorSelection.cursor(pos), false).head;
}

// WHY: Soft wrap may break right after a space; moveToLineBoundary returns the
// position after that trailing whitespace. UX expectation is "end of visible
// text", so step back through whitespace — but never past line.from, and never
// when we're already at the physical line end (logical end of paragraph).
function trimTrailingSpaceBack(state: EditorState, pos: number, lineFrom: number, lineTo: number): number {
  if (pos >= lineTo) return pos;
  let p = pos;
  while (p > lineFrom && /\s/.test(state.doc.sliceString(p - 1, p))) p--;
  return p;
}

// Build waypoints in document order (ascending). Cmd+→ picks the smallest > pos;
// Cmd+← picks the largest < pos. Duplicates are deduplicated. This gives a clean
// monotonic stepping that collapses to fewer stops when waypoints coincide
// (e.g. unwrapped lines: visStart === lineFrom, visEnd === lineTo).
function endWaypoints(view: EditorView, pos: number): number[] {
  const state = view.state;
  const line = state.doc.lineAt(pos);
  const visEnd = trimTrailingSpaceBack(state, visualLineEnd(view, pos), line.from, line.to);
  // Exclude the list/heading marker region from delimiter scanning so marker
  // chars (e.g. "." in "1.") don't become spurious content stops.
  const listContentStart = findListItemAt(state, pos) !== null ? findContentStart(state, line.from) : null;
  const headingContentStart = findHeadingAt(state, pos) !== null ? findHeadingContentStart(state, line.from) : null;
  const scanFrom = listContentStart ?? headingContentStart ?? line.from;
  const points = new Set<number>();
  if (visEnd > line.from && visEnd <= line.to) points.add(visEnd);
  for (const p of delimiterWaypointsRight(line, scanFrom)) if (p > line.from && p <= line.to) points.add(p);
  points.add(line.to);
  return Array.from(points).sort((a, b) => a - b);
}

function startWaypoints(view: EditorView, pos: number): number[] {
  const state = view.state;
  const line = state.doc.lineAt(pos);
  const visStart = visualLineStart(view, pos);
  const isList = findListItemAt(state, pos) !== null;
  const isHeading = findHeadingAt(state, pos) !== null;
  const listContentStart = isList ? findContentStart(state, line.from) : null;
  const headingContentStart = isHeading ? findHeadingContentStart(state, line.from) : null;
  const contentStart = listContentStart ?? headingContentStart;
  const points = new Set<number>();
  points.add(line.from);
  if (contentStart !== null && contentStart > line.from) points.add(contentStart);
  for (const p of delimiterWaypointsLeft(line, contentStart ?? line.from)) if (p >= line.from && p <= line.to) points.add(p);
  if (visStart >= line.from && visStart <= line.to) points.add(visStart);
  return Array.from(points).sort((a, b) => a - b);
}

function nextRight(points: number[], pos: number): number | null {
  for (const p of points) if (p > pos) return p;
  return null;
}

function nextLeft(points: number[], pos: number): number | null {
  for (let i = points.length - 1; i >= 0; i--) if (points[i] < pos) return points[i];
  return null;
}

export function cursorLineEndStep(view: EditorView): boolean {
  const pos = view.state.selection.main.head;
  const target = nextRight(endWaypoints(view, pos), pos);
  if (target === null) return false;
  view.dispatch({ selection: { anchor: target } });
  return true;
}

export function selectLineEndStep(view: EditorView): boolean {
  const range = view.state.selection.main;
  const target = nextRight(endWaypoints(view, range.head), range.head);
  if (target === null) return false;
  view.dispatch({ selection: EditorSelection.range(range.anchor, target) });
  return true;
}

export function cursorLineStartStep(view: EditorView): boolean {
  const pos = view.state.selection.main.head;
  const target = nextLeft(startWaypoints(view, pos), pos);
  if (target === null) return false;
  view.dispatch({ selection: { anchor: target } });
  return true;
}

export function selectLineStartStep(view: EditorView): boolean {
  const range = view.state.selection.main;
  const target = nextLeft(startWaypoints(view, range.head), range.head);
  if (target === null) return false;
  view.dispatch({ selection: EditorSelection.range(range.anchor, target) });
  return true;
}

export function arrowDownIntoList(view: EditorView): boolean {
  const state = view.state;
  const pos = state.selection.main.head;
  const line = state.doc.lineAt(pos);

  if (line.number >= state.doc.lines) return false;

  const nextLine = state.doc.line(line.number + 1);
  if (findListItemAt(state, nextLine.from) === null) return false;

  const contentStart = findContentStart(state, nextLine.from);
  if (contentStart === null) return false;

  const col = pos - line.from;
  const targetCol = Math.min(col, nextLine.length);
  const rawTarget = nextLine.from + targetCol;

  if (rawTarget < contentStart) {
    view.dispatch({ selection: { anchor: contentStart } });
    return true;
  }

  return false;
}

export function arrowUpIntoList(view: EditorView): boolean {
  const state = view.state;
  const pos = state.selection.main.head;
  const line = state.doc.lineAt(pos);

  if (line.number <= 1) return false;

  const prevLine = state.doc.line(line.number - 1);
  if (findListItemAt(state, prevLine.from) === null) return false;

  const contentStart = findContentStart(state, prevLine.from);
  if (contentStart === null) return false;

  const col = pos - line.from;
  const targetCol = Math.min(col, prevLine.length);
  const rawTarget = prevLine.from + targetCol;

  if (rawTarget < contentStart) {
    view.dispatch({ selection: { anchor: contentStart } });
    return true;
  }

  return false;
}

export function arrowLeftAtListContentStart(view: EditorView): boolean {
  const state = view.state;
  const pos = state.selection.main.head;
  const line = state.doc.lineAt(pos);

  if (findListItemAt(state, pos) === null) return false;

  const contentStart = findContentStart(state, line.from);
  if (contentStart === null) return false;

  if (pos !== contentStart) return false;

  view.dispatch({ selection: { anchor: line.from } });
  return true;
}

export const listNavExtension: Extension = Prec.high(
  keymap.of([
    { key: 'Mod-ArrowLeft', run: cursorLineStartStep, shift: selectLineStartStep },
    { key: 'Mod-ArrowRight', run: cursorLineEndStep, shift: selectLineEndStep },
    { key: 'ArrowDown', run: arrowDownIntoList },
    { key: 'ArrowUp', run: arrowUpIntoList },
    { key: 'ArrowLeft', run: arrowLeftAtListContentStart },
  ]),
);
