/**
 * Atomic blocks — images and table anchors as whole objects in the document editor.
 *
 * SYSTEM: atomic-delete-guard — transaction guard + whole-object selection
 * affordances for atomic objects (`atomicObjectField`: image embeds and table anchors).
 *
 * PINNED USER RULE (verbatim): «мы не удаляем таблицу/картинку одним символом —
 * только выделив её целиком» — a table/image is never deleted by a single character,
 * only by selecting it whole.
 *
 * Extensions, all mounted inside `renderExtensions()` (render-bundle.ts) so
 * Mod-/ (plain-text mode) detaches them together with the widgets — raw text stays
 * freely editable there:
 *  - `atomicDeleteGuard` — an `EditorState.transactionFilter` guarding BOTH:
 *     - DROPS a one-key delete (any `delete.*` user event) whose change overlaps
 *       or line-joins into a guarded range while the START selection is empty.
 *       Removing an empty line beside a table joins nothing and passes.
 *     - REWRITES an input/paste/drop/complete transaction (every `input.*` user
 *       event except `input.type.compose`) that would glue text onto a table
 *       anchor LINE: the anchor line's text stays byte-identical, the inserted
 *       text lands on its own adjacent line, and any deleted span touching the
 *       anchor or its join newline is kept (dropped from the deletion).
 *       Remote Yjs transactions (no user event) and IME composition
 *       (`input.type.compose` — rewriting mid-composition breaks the IME
 *       session) pass through untouched.
 *    A non-empty start selection covering the range whole is the intended
 *    replace gesture and always passes. NOT a keymap: the filter covers every
 *    command and DOM-input path at once.
 *  - `atomicCompositionGuard` — a `compositionstart` DOM handler that, at a
 *    table-anchor edge, opens a fresh line and lands the caret on it BEFORE any
 *    composed text exists (compose transactions are never rewritten, so the IME
 *    text must never start at the edge).
 *  - `atomicSelectedField` — a `cm-atomic-selected` mark decoration over every
 *    guarded range fully covered by a non-empty selection. It does not reach a
 *    replaced widget; the frame is the plugin below.
 *  - `atomicFramePlugin` — toggles the kind's selected class on the object's DOM.
 *  - `atomicKeymap` — keyboard contract of a whole-selected image or table:
 *    Enter opens a line after it (never deletes it), Esc collapses to its end.
 *  - `atomicFocusRelease` — a view losing focus collapses its whole-selected image.
 */
// see SYSTEM: live-preview — guarded ranges are read from atomicObjectField directly,
// NOT via the EditorView.atomicRanges facet: the facet also carries checkboxes (whose
// delete contract is the opposite — Backspace removes them whole) and its values are
// `view => RangeSet` providers, awkward to read from a state-only transaction filter.

import { syntaxTree } from '@codemirror/language';
import { Decoration, DecorationSet, EditorView, ViewPlugin, keymap, type ViewUpdate } from '@codemirror/view';
import {
  EditorState,
  Prec,
  Range,
  StateField,
  ChangeSet,
  Transaction,
  type Text,
  type TransactionSpec,
} from '@codemirror/state';
import { atomicObjectField } from './fields';

// ─── Guarded ranges ───────────────────────────────────────────────────────────

export type AtomicKind = 'image' | 'table';

export interface GuardedRange {
  from: number;
  to: number;
  /**
   * True for whole-line block anchors (table blocks): besides overlap, the guard
   * also swallows a delete that JOINS the anchor's line with a neighbor — the
   * whole-line replace shape (fields.ts) requires the anchor to stay standalone.
   * Inline image embeds keep single-character deletes around them legal.
   */
  lineBlock: boolean;
  kind: AtomicKind;
}

/**
 * The widget instance behind a decoration — the one typed read of CM6's opaque
 * `Decoration.spec`; every widget instanceof-check goes through here.
 */
export function widgetOf(dec: Decoration): unknown {
  return (dec as unknown as { spec: { widget?: unknown } }).spec.widget;
}

/** The atomic kind a decoration was built with (`spec.atomicKind`). */
function kindOf(dec: Decoration): AtomicKind {
  return (dec as unknown as { spec: { atomicKind: AtomicKind } }).spec.atomicKind;
}

/**
 * Guarded atomic ranges in `state`, from atomicObjectField. Read with `false` so a state
 * without it (plain-text mode, unit harnesses) yields no guards instead of throwing.
 */
export function guardedAtomicRanges(state: EditorState): GuardedRange[] {
  const out: GuardedRange[] = [];
  state.field(atomicObjectField, false)?.between(0, state.doc.length, (from, to, dec) => {
    const kind = kindOf(dec);
    out.push({ from, to, kind, lineBlock: kind === 'table' });
  });
  return out;
}

/**
 * The guarded range the main selection EXACTLY equals (a whole-selected image or
 * table), or null. Image-only callers check `kind`.
 */
export function selectedAtomicRange(state: EditorState): GuardedRange | null {
  const sel = state.selection.main;
  if (sel.empty) return null;
  return guardedAtomicRanges(state).find((g) => sel.from === g.from && sel.to === g.to) ?? null;
}

/** The guarded range containing `pos`, or null. Image-only callers check `kind`. */
export function atomicRangeAt(state: EditorState, pos: number): GuardedRange | null {
  // WHY half-open: touching embeds share a boundary (`![a]…![b]…`) and posAtDOM yields
  // a widget's START — a closed range resolves the second image to the first.
  return guardedAtomicRanges(state).find((g) => g.from <= pos && pos < g.to) ?? null;
}

// ─── Delete guard ─────────────────────────────────────────────────────────────

/** Does the deleted span [from, to) hit the guarded range `g` in `doc`? */
function hitsGuard(doc: Text, from: number, to: number, g: GuardedRange): boolean {
  // Overlap: the delete consumes part of the guarded range (CM6 atomic deleteBy
  // widens a one-key delete at the range edge to the WHOLE atom — overlap is how
  // that lands here).
  if (from < g.to && to > g.from) return true;
  if (!g.lineBlock) return false;
  // Line join: deleting the line break at the guard's line end, or the one before
  // its line start, merges the standalone anchor line into a neighbor and breaks
  // the whole-line replace shape.
  if (from === g.to && to > from && g.to < doc.length && doc.sliceString(g.to, g.to + 1) === '\n') return true;
  if (to === g.from && from < to && g.from > 0 && doc.sliceString(g.from - 1, g.from) === '\n') return true;
  return false;
}

/**
 * Is the deleted span only line breaks, with one side of it an empty line? Nothing
 * joins then: every anchor stays alone on its line.
 * INVARIANT: an empty line next to a table is deletable from the line itself and from
 * the table's edge, and two tables can stand with no line between them.
 * Why: the operator rejected a blank line that no key can remove.
 */
function removesEmptyLine(doc: Text, from: number, to: number): boolean {
  if (!/^\n+$/.test(doc.sliceString(from, to))) return false;
  const emptyBefore = from === 0 || doc.sliceString(from - 1, from) === '\n';
  const emptyAfter = to === doc.length || doc.sliceString(to, to + 1) === '\n';
  return emptyBefore || emptyAfter;
}

/**
 * The parts of the deleted span [from, to) that do NOT touch the anchor or its
 * join newlines (those parts stay deleted in a rewritten transaction; the rest of
 * the span is kept so the anchor line survives byte-identical).
 */
function keptAnchorLineDeletes(
  doc: Text,
  from: number,
  to: number,
  g: GuardedRange,
): { from: number; to: number }[] {
  const spans: { from: number; to: number }[] = [];
  let cur = from;
  const cut = (until: number) => { if (cur < until) spans.push({ from: cur, to: until }); };

  // Protected zones in POSITION order (the join newline before the anchor
  // line, the anchor range itself, the join newline after it) — each zone that
  // the deleted span reaches is skipped whole. WHY position order, not category
  // order: an overlap-first cut let a deletion spanning [before the line … into
  // the anchor] emit the LEADING join newline as a deletable span before the
  // join check ran — a silent line merge. Unreachable through user gestures
  // today (atomicRanges extends any crossing selection to cover the anchor
  // whole, which skips the rewrite); this guards future changes to that skip.
  const zones: { zFrom: number; zTo: number }[] = [
    { zFrom: g.from, zTo: g.to },
  ];
  if (g.from > 0 && doc.sliceString(g.from - 1, g.from) === '\n') {
    zones.unshift({ zFrom: g.from - 1, zTo: g.from });
  }
  if (g.to < doc.length && doc.sliceString(g.to, g.to + 1) === '\n') {
    zones.push({ zFrom: g.to, zTo: g.to + 1 });
  }
  for (const { zFrom, zTo } of zones) {
    if (zFrom >= to || zTo <= from) continue; // zone untouched by this deletion
    cut(zFrom);
    cur = Math.max(cur, zTo);
  }
  cut(to);
  return spans;
}

/**
 * INVARIANT: a local input transaction never changes a table anchor LINE's text
 * unless its start selection covers the anchor whole (typing over a selected
 * table replaces it). Why: the anchor line is whole-line-replaced by the
 * widget, so text glued onto it is silently HIDDEN — characters vanish for the
 * user until plain-text mode.
 *
 * Rewrites the first firing change of `tr`: the deleted part touching the anchor
 * or its join newline is dropped, the inserted text goes on its own line — after
 * the anchor line (`\n` + text) when the change sits at/after `g.to`, before it
 * (text + `\n`) when at `g.from` — and the caret lands after the inserted text.
 * Non-firing changes (e.g. the delete half of a move-drop) pass verbatim.
 * Returns null when nothing fires (pass the transaction through).
 */
function rewriteAnchorLineInput(tr: Transaction, guards: GuardedRange[]): TransactionSpec | null {
  const doc = tr.startState.doc;
  const sel = tr.startState.selection.main;

  // Collect first (iterChanges only offers a callback); the firing analysis runs
  // in a plain loop so control-flow narrowing stays sound.
  const changesList: { fromA: number; toA: number; text: string }[] = [];
  tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    changesList.push({ fromA, toA, text: inserted.toString() });
  });

  const keep: { from: number; to?: number; insert?: string }[] = [];
  let fired: GuardedRange | null = null;
  let fromA = 0;
  let toA = 0;
  let insertText = '';
  let multiple = false;

  for (const ch of changesList) {
    let hit: GuardedRange | null = null;
    for (const g of guards) {
      if (!g.lineBlock) continue;
      // The replace gesture passes: a start selection covering the anchor whole.
      if (!sel.empty && sel.from <= g.from && sel.to >= g.to) continue;
      const deletesIntoAnchor = ch.toA > ch.fromA && hitsGuard(doc, ch.fromA, ch.toA, g);
      const insertsAtEdge = ch.fromA === ch.toA && (ch.fromA === g.from || ch.fromA === g.to);
      if (deletesIntoAnchor || insertsAtEdge) { hit = g; break; }
    }
    if (hit) {
      if (fired) multiple = true; // not a real gesture — no rewrite (runs verbatim)
      else { fired = hit; fromA = ch.fromA; toA = ch.toA; insertText = ch.text; }
      continue;
    }
    keep.push(ch.toA > ch.fromA
      ? { from: ch.fromA, to: ch.toA, insert: ch.text }
      : { from: ch.fromA, insert: ch.text });
  }

  if (!fired || multiple) return null;

  const g = fired;
  const dels = keptAnchorLineDeletes(doc, fromA, toA, g);
  // Side: at/after the anchor line end the text goes BELOW it; at its start, ABOVE.
  // A change starting strictly INSIDE the hidden anchor text also goes below
  // (reading order — such a selection is only reachable by mouse across hidden
  // text, never by keyboard: the anchor range is atomic).
  const after = fromA >= g.to || toA > g.to || fromA > g.from;
  const insertAt = after ? g.to : g.from;
  let inserted = insertText;
  if (insertText) {
    // Enter / newline-led paste already carry the break — never double it.
    if (after && !inserted.startsWith('\n')) inserted = '\n' + inserted;
    if (!after && !inserted.endsWith('\n')) inserted = inserted + '\n';
  }

  const changes: { from: number; to?: number; insert?: string }[] = [...keep, ...dels];
  if (inserted) changes.push({ from: insertAt, insert: inserted });
  const cs = ChangeSet.of(changes, doc.length);
  const caret = after
    ? cs.mapPos(insertAt, 1) // past the break + inserted text
    // before the trailing break, whether it was added here or the input carried it
    : cs.mapPos(insertAt, -1) + insertText.length - (insertText.endsWith('\n') ? 1 : 0);
  return {
    changes,
    selection: { anchor: caret },
    scrollIntoView: true,
    userEvent: tr.annotation(Transaction.userEvent) ?? undefined,
  };
}

/**
 * The transaction guard proper: one-key deletes into a guarded range are DROPPED
 * (empty start selection only), input transactions that would glue onto a table
 * anchor line are REWRITTEN. Everything else — selection deletes/replaces over a
 * whole-covered atom, remote Yjs edits, undo, compose — passes untouched.
 */
export const atomicDeleteGuard = EditorState.transactionFilter.of((tr) => {
  const isDelete = tr.isUserEvent('delete');
  // input.* covers type/paste/drop/complete/newline; composition is excluded —
  // rewriting mid-composition breaks the IME session (atomicCompositionGuard
  // pre-empts the anchor-edge case instead).
  const isInput = !isDelete && tr.isUserEvent('input') && !tr.isUserEvent('input.type.compose');
  if (!isDelete && !isInput) return tr;

  const guards = guardedAtomicRanges(tr.startState);
  if (!guards.length) return tr;

  if (isDelete) {
    // A non-empty start selection is the intended removal gesture («только выделив
    // её целиком»); cut over a real selection (`delete.cut`) passes here too.
    if (!tr.startState.selection.main.empty) return tr;
    const doc = tr.startState.doc;
    let drop = false;
    tr.changes.iterChanges((fromA, toA) => {
      if (drop || fromA === toA || removesEmptyLine(doc, fromA, toA)) return;
      for (const g of guards) {
        if (hitsGuard(doc, fromA, toA, g)) {
          drop = true;
          return;
        }
      }
    });
    return drop ? [] : tr;
  }

  return rewriteAnchorLineInput(tr, guards) ?? tr;
});

// ─── IME composition at an anchor edge ────────────────────────────────────────

/**
 * Composition transactions are never rewritten (an IME session cannot survive a
 * transaction filter swapping the text under it), so a composition STARTING at a
 * table-anchor edge would glue its text onto the anchor line. This handler runs
 * at `compositionstart`, before any composed text exists: it opens a line on the
 * outward side of the anchor and lands the caret on it — the composition then
 * proceeds normally at the moved caret.
 */
export const atomicCompositionGuard = EditorView.domEventHandlers({
  compositionstart(_event, view) {
    const sel = view.state.selection.main;
    if (!sel.empty) return false;
    for (const g of guardedAtomicRanges(view.state)) {
      if (!g.lineBlock) continue;
      let side: 'before' | 'after';
      if (sel.head === g.to) side = 'after';
      else if (sel.head === g.from) side = 'before';
      else continue;
      // No userEvent: this must not re-enter the input rewrite or group with the
      // composed text in history — it is a programmatic line open, not typing.
      view.dispatch({
        changes: { from: side === 'after' ? g.to : g.from, insert: '\n' },
        selection: { anchor: side === 'after' ? g.to + 1 : g.from },
        scrollIntoView: true,
      });
      return false; // let the composition continue — now at the moved caret
    }
    return false;
  },
});

// ─── Focus leaving a selected image ───────────────────────────────────────────

/**
 * Collapses `view`'s whole-selected image once focus has settled outside the view.
 * WHY deferred: a blur fires mid-transfer, before focus has landed anywhere.
 * WHY activeElement, not view.hasFocus: hasFocus is also false after switching to another
 * window, and that leaves activeElement on the content DOM — coming back keeps the selection.
 * The image toolbar works on the selected image and a modal hands focus back on close
 * (the preview lightbox), so focus inside either keeps the selection.
 */
export function releaseSelectedImage(view: EditorView): void {
  setTimeout(() => {
    if (!view.dom.isConnected) return;
    const active = (view.root as Document | ShadowRoot).activeElement;
    if (active === view.contentDOM || active?.closest('.selection-toolbar, [role="dialog"]')) return;
    const g = selectedAtomicRange(view.state);
    // INVARIANT: любой клик вне картинки, даже если она в таблице, снимает выделение с картинки.
    // Why: every table cell is its own view with its own selection, so a click in another
    // cell leaves the image selected and the next press on it opens the preview at once.
    if (g?.kind === 'image') view.dispatch({ selection: { anchor: g.to } });
  }, 0);
}

export const atomicFocusRelease = EditorView.domEventHandlers({
  blur(_event, view) {
    releaseSelectedImage(view);
    return false;
  },
});

// ─── Selected-block highlight ─────────────────────────────────────────────────

function buildAtomicSelected(state: EditorState): DecorationSet {
  const sel = state.selection.main;
  if (sel.empty) return Decoration.none;
  const ranges: Range<Decoration>[] = [];
  for (const g of guardedAtomicRanges(state)) {
    if (sel.from <= g.from && sel.to >= g.to) {
      ranges.push(Decoration.mark({ class: 'cm-atomic-selected' }).range(g.from, g.to));
    }
  }
  return ranges.length ? Decoration.set(ranges, true) : Decoration.none;
}

/**
 * `cm-atomic-selected` mark over each guarded range fully covered by the
 * selection. The mark does not reach a replaced widget: atomicFramePlugin frames it.
 */
export const atomicSelectedField = StateField.define<DecorationSet>({
  create(state) { return buildAtomicSelected(state); },
  update(decs, tr) {
    if (tr.docChanged || tr.selection
        || syntaxTree(tr.state) !== syntaxTree(tr.startState)) {
      return buildAtomicSelected(tr.state);
    }
    return decs;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// ─── Whole-object frame + Esc ─────────────────────────────────────────────────

const FRAME_CLASS = { image: 'cm-image-embed--selected', table: 'cm-table-block-wrap--selected' };
const OBJECT_SELECTOR: Record<string, AtomicKind> = { '.cm-image-embed': 'image', '.cm-table-block-wrap': 'table' };

/**
 * Toggles the kind's selected class on each object fully covered by the selection: the
 * frame, the visible handles and the zoom-in cursor key on it.
 * WHY a ViewPlugin and not a widget spec flag: a selected flag in the spec would force a
 * new widget instance per select, and on DOM reuse CM6 never calls `destroy()` on the old
 * stateful instance (Yjs observers, cell views would leak). Re-derived on any transaction
 * and viewport change, so a widget rebuilt or evicted-and-remounted regains its frame.
 */
export const atomicFramePlugin = ViewPlugin.fromClass(
  class {
    private hadSelected = false;

    constructor(view: EditorView) { this.sync(view); }

    update(u: ViewUpdate) {
      if (u.transactions.length > 0 || u.viewportChanged) this.sync(u.view);
    }

    private sync(view: EditorView) {
      const sel = view.state.selection.main;
      const selected = new Set<number>();
      if (!sel.empty) {
        for (const g of guardedAtomicRanges(view.state)) {
          if (sel.from <= g.from && sel.to >= g.to) selected.add(g.from);
        }
      }
      // Nothing selected now or before → skip the DOM scan: runs on every keystroke.
      if (!selected.size && !this.hadSelected) return;
      this.hadSelected = selected.size > 0;
      for (const [selector, kind] of Object.entries(OBJECT_SELECTOR)) {
        view.contentDOM.querySelectorAll<HTMLElement>(selector).forEach((el) => {
          // A nested view owns its objects.
          if (el.closest('.cm-editor') !== view.dom) return;
          let pos = -1;
          try { pos = view.posAtDOM(el); } catch { /* detached */ }
          el.classList.toggle(FRAME_CLASS[kind], selected.has(pos));
        });
      }
    }
  },
);

/**
 * Keyboard contract of a whole-selected atomic object (image or table) — ONE rule
 * for every such object, never a per-type keymap:
 *  - Enter moves past the object and opens a new line there.
 *    INVARIANT: Enter never deletes a selected object; only Backspace/Delete on the
 *    whole selection removes it.
 *    Why: a selected object is the normal state after a click or Shift+Arrow; Enter there means "new line".
 *  - Esc collapses the selection to the object's end (arrow keys collapse natively).
 */
export const atomicKeymap = Prec.high(keymap.of([
  {
    key: 'Enter',
    run: (view) => {
      const g = selectedAtomicRange(view.state);
      if (!g) return false;
      view.dispatch({
        changes: { from: g.to, insert: '\n' },
        selection: { anchor: g.to + 1 },
        scrollIntoView: true,
        userEvent: 'input',
      });
      return true;
    },
  },
  {
    key: 'Escape',
    run: (view) => {
      const g = selectedAtomicRange(view.state);
      if (!g) return false;
      view.dispatch({ selection: { anchor: g.to } });
      return true;
    },
  },
]));
