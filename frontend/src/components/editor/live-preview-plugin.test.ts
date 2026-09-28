/** Unit tests for live-preview-plugin — CM6 decorations in jsdom. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { EditorState, type Extension, type StateField } from '@codemirror/state';
import { Decoration, EditorView, type DecorationSet } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { livePreviewField, listLinePlugin, cursorLinePlugin, tableRenderField, mathBlockRenderField, mermaidBlockRenderField, transcludeMap, parseSizeFromAlt, validDocIds, validNoteThreadIds, validRefIds, transclusionDepth, revealAtCursor } from './live-preview';
import { resolveTransclusion } from './live-preview';
import { TransclusionWidget } from './live-preview/widgets';
import { LIST_STEP, LIST_GUTTER, BASE_PADDING } from './live-preview/list-indent';

// ── Helpers ──────────────────────────────────────────────────────────────────

function createView(doc: string, cursor: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor: cursor },
    extensions: [markdown({ base: markdownLanguage }), livePreviewField, listLinePlugin, cursorLinePlugin],
  });
  const view = new EditorView({ state, parent: document.createElement('div') });
  // Force synchronous parse so syntax tree is available for decorations.
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  // Trigger decoration rebuild after tree is ready.
  view.dispatch({ selection: { anchor: cursor } });
  return view;
}

/** Collect all decorations from both StateField and ViewPlugin as a flat array. */
function getDecorations(view: EditorView) {
  const decs: { from: number; to: number; spec: Record<string, unknown> }[] = [];
  const collect = (set: { between(from: number, to: number, cb: (from: number, to: number, dec: Decoration) => void): void }) => {
    set.between(0, view.state.doc.length, (from, to, dec) => {
      decs.push({ from, to, spec: (dec as unknown as { spec: Record<string, unknown> }).spec });
    });
  };
  // Structural decorations from StateField
  collect(view.state.field(livePreviewField));
  // Line decorations from ViewPlugins
  const listPlugin = view.plugin(listLinePlugin);
  if (listPlugin) collect(listPlugin.decorations);
  const cursorPlugin = view.plugin(cursorLinePlugin);
  if (cursorPlugin) collect(cursorPlugin.decorations);
  return decs;
}

function hasReplaceDec(view: EditorView): boolean {
  return getDecorations(view).some((d) => {
    const spec = d.spec as Record<string, unknown>;
    // Decoration.replace sets isReplace or has no class
    return !('class' in spec) || spec.isReplace;
  });
}

/** Count decorations that are Decoration.replace (no class property, or widget). */
function replaceDecorations(view: EditorView) {
  return getDecorations(view).filter((d) => {
    const spec = d.spec as Record<string, unknown>;
    return !('class' in spec);
  });
}

/** Count decorations that are Decoration.line containing a specific class. */
function lineDecorations(view: EditorView, cls: string) {
  return getDecorations(view).filter((d) => {
    const spec = d.spec as Record<string, unknown>;
    const specClass = spec.class;
    return typeof specClass === 'string' && specClass.split(' ').includes(cls);
  });
}

/** Count decorations that are Decoration.mark with a specific class. */
function markDecorations(view: EditorView, cls: string) {
  return getDecorations(view).filter((d) => {
    const spec = d.spec as Record<string, unknown>;
    return (spec as Record<string, unknown>).class === cls && d.from !== d.to;
  });
}


// ── Setup ────────────────────────────────────────────────────────────────────

beforeEach(() => {
  transcludeMap.clear();
  validDocIds.clear();
  validNoteThreadIds.clear();
  validRefIds.clear();
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('marker hiding — emphasis', () => {
  it('hides bold markers when cursor is outside', () => {
    // Cursor at end of doc, outside the bold span
    const view = createView('hello **world** end', 19);
    const reps = replaceDecorations(view);
    // Should have replace decorations for the ** markers
    const inBoldRange = reps.filter((d) => d.from >= 6 && d.to <= 15);
    expect(inBoldRange.length).toBeGreaterThan(0);
  });

  it('shows bold markers when cursor is inside', () => {
    // Cursor inside the bold text
    const view = createView('hello **world** end', 10);
    const reps = replaceDecorations(view);
    // No replace decorations within the bold range (markers visible)
    const inBoldRange = reps.filter((d) => d.from >= 6 && d.to <= 15);
    expect(inBoldRange).toHaveLength(0);
  });

  it('hides italic markers when cursor is outside', () => {
    const view = createView('hello *world* end', 17);
    const reps = replaceDecorations(view);
    const inItalicRange = reps.filter((d) => d.from >= 6 && d.to <= 13);
    expect(inItalicRange.length).toBeGreaterThan(0);
  });

  it('shows italic markers when cursor is inside', () => {
    const view = createView('hello *world* end', 9);
    const reps = replaceDecorations(view);
    const inItalicRange = reps.filter((d) => d.from >= 6 && d.to <= 13);
    expect(inItalicRange).toHaveLength(0);
  });
});

describe('marker hiding — headings', () => {
  it('hides heading marker when cursor is on different line', () => {
    const view = createView('# Title\nBody text', 13);
    const reps = replaceDecorations(view);
    // HeaderMark (# ) should be hidden
    const headingReps = reps.filter((d) => d.from === 0 && d.to <= 2);
    expect(headingReps.length).toBeGreaterThan(0);
  });

  it('shows heading marker when cursor is on heading line', () => {
    const view = createView('# Title\nBody text', 3);
    const reps = replaceDecorations(view);
    const headingReps = reps.filter((d) => d.from === 0 && d.to <= 2);
    expect(headingReps).toHaveLength(0);
  });

  it('nested view (revealAtCursor=false) hides the heading marker even with the cursor on it', () => {
    // A nested transclusion view defaults its selection to position 0; it must NOT reveal
    // the raw `# ` of a leading heading (reveal-on-cursor is an editing affordance only).
    // Driven by the revealAtCursor facet (false in nested views), not transclusionDepth.
    const state = EditorState.create({
      doc: '# Title\nBody text',
      selection: { anchor: 0 },
      extensions: [
        markdown({ base: markdownLanguage }),
        livePreviewField, listLinePlugin, cursorLinePlugin,
        revealAtCursor.of(false),
      ],
    });
    const view = new EditorView({ state, parent: document.createElement('div') });
    ensureSyntaxTree(view.state, view.state.doc.length, 5000);
    view.dispatch({ selection: { anchor: 0 } });
    const reps = replaceDecorations(view);
    const headingReps = reps.filter((d) => d.from === 0 && d.to <= 2);
    expect(headingReps.length).toBeGreaterThan(0);
  });
});

describe('marker hiding — blockquote', () => {
  it('hides quote marker when cursor is outside', () => {
    // Blank line separates blockquote from paragraph so cursor is clearly outside.
    const view = createView('> quoted\n\nunquoted', 15);
    const allDecs = getDecorations(view);
    // QuoteMark at position 0 should be replaced (hidden)
    const quoteReps = allDecs.filter((d) => d.from === 0 && d.to <= 2 && !('class' in d.spec));
    expect(quoteReps.length).toBeGreaterThan(0);
  });

  it('shows quote marker when cursor is inside', () => {
    const view = createView('> quoted\n\nunquoted', 4);
    const allDecs = getDecorations(view);
    const quoteReps = allDecs.filter((d) => d.from === 0 && d.to <= 2 && !('class' in d.spec));
    expect(quoteReps).toHaveLength(0);
  });
});

describe('links', () => {
  it('hides link syntax when cursor is outside', () => {
    const view = createView('see [text](http://x.com) end', 28);
    const reps = replaceDecorations(view);
    // Should have decorations hiding [ and ](url)
    const linkReps = reps.filter((d) => d.from >= 4 && d.to <= 24);
    expect(linkReps.length).toBeGreaterThan(0);
  });

  it('shows all link syntax when cursor is inside', () => {
    const view = createView('see [text](http://x.com) end', 8);
    const reps = replaceDecorations(view);
    const linkReps = reps.filter((d) => d.from >= 4 && d.to <= 24);
    expect(linkReps).toHaveLength(0);
  });
});

describe('double-bracket links — pattern 1 [[text]](url)', () => {
  it('applies cm-doc-link mark and hides outer syntax when cursor is outside', () => {
    validDocIds.clear();
    validDocIds.add('abc-123');
    const doc = 'see [[page]](abc-123) end';
    const view = createView(doc, doc.length - 1);
    const marks = markDecorations(view, 'cm-doc-link');
    expect(marks.length).toBeGreaterThan(0);
    const reps = replaceDecorations(view);
    const hidingOuterBracket = reps.find((d) => d.from === 4 && d.to === 5);
    const hidingUrlPart = reps.find((d) => d.from > 10 && d.from < 15);
    expect(hidingOuterBracket).toBeDefined();
    expect(hidingUrlPart).toBeDefined();
  });

  it('reveals raw markdown when cursor is inside', () => {
    const doc = 'see [[page]](abc-123) end';
    const view = createView(doc, 8);
    const reps = replaceDecorations(view);
    const linkReps = reps.filter((d) => d.from >= 4 && d.to < 20);
    expect(linkReps).toHaveLength(0);
  });

  it('applies cm-doc-link-broken mark when doc id is not in validDocIds', () => {
    const doc = 'see [[page]](missing-id) end';
    const view = createView(doc, doc.length - 1);
    const brokenMarks = markDecorations(view, 'cm-doc-link-broken');
    expect(brokenMarks.length).toBeGreaterThan(0);
    const validMarks = markDecorations(view, 'cm-doc-link');
    expect(validMarks).toHaveLength(0);
  });
});

describe('double-bracket links — pattern 2 [[text](url)]', () => {
  it('applies cm-ext-link mark and keeps outer brackets visible when cursor is outside', () => {
    const doc = 'see [[text](http://x.com)] end';
    const view = createView(doc, doc.length - 1);
    const marks = markDecorations(view, 'cm-ext-link');
    expect(marks.length).toBeGreaterThan(0);
    const reps = replaceDecorations(view);
    const hidingOuterOpen = reps.find((d) => d.from === 4 && d.to === 5);
    const hidingOuterClose = reps.find((d) => d.from === 25 && d.to === 26);
    expect(hidingOuterOpen).toBeUndefined();
    expect(hidingOuterClose).toBeUndefined();
  });

  it('reveals raw markdown when cursor is on outer bracket', () => {
    const doc = 'see [[text](http://x.com)] end';
    const view = createView(doc, 4);
    const reps = replaceDecorations(view);
    const linkReps = reps.filter((d) => d.from >= 5 && d.to <= 25);
    expect(linkReps).toHaveLength(0);
  });

  it('reveals raw markdown when cursor is inside link', () => {
    const doc = 'see [[text](http://x.com)] end';
    const view = createView(doc, 10);
    const reps = replaceDecorations(view);
    const linkReps = reps.filter((d) => d.from >= 5 && d.to <= 25);
    expect(linkReps).toHaveLength(0);
  });
});

describe('horizontal rule', () => {
  it('hides HR and adds line class when cursor is outside', () => {
    // Blank lines around --- ensure it's parsed as HorizontalRule, not setext heading.
    const view = createView('above\n\n---\n\nbelow', 0);
    const allDecs = getDecorations(view);
    // HR content (position 7-10) should have a replace decoration
    const hrReps = allDecs.filter((d) => d.from >= 7 && d.to <= 10 && !('class' in d.spec));
    expect(hrReps.length).toBeGreaterThan(0);
    const hrLines = lineDecorations(view, 'cm-hr-line');
    expect(hrLines.length).toBeGreaterThan(0);
  });

  it('shows HR syntax when cursor is on HR line', () => {
    const view = createView('above\n\n---\n\nbelow', 8);
    const allDecs = getDecorations(view);
    const hrReps = allDecs.filter((d) => d.from >= 7 && d.to <= 10 && !('class' in d.spec));
    expect(hrReps).toHaveLength(0);
  });
});

describe('fenced code blocks', () => {
  it('adds cm-fenced-code line decorations when cursor is outside', () => {
    const doc = 'text\n\n```\ncode here\n```\n\nend';
    const view = createView(doc, doc.length - 1); // cursor on "end"
    const codeLines = lineDecorations(view, 'cm-fenced-code');
    expect(codeLines.length).toBeGreaterThanOrEqual(1);
  });

  it('adds --editing modifier to fence lines when cursor is inside block', () => {
    const doc = 'text\n\n```\ncode here\n```\n\nend';
    const codeStart = doc.indexOf('code here');
    const view = createView(doc, codeStart + 2);
    const fenceLines = lineDecorations(view, 'cm-fenced-code-fence--editing');
    expect(fenceLines).toHaveLength(2); // opening and closing fence
  });

  // F3 (charWidth guard): the raw fence marker lines (``` / ```lang) render in the
  // CODE font; a short pure-ASCII line in code font is a valid charWidth sample for
  // CM6's measureTextSize() and re-triggers the 2026-05-30 scroll-jump. They must be
  // hidden (replaced) when the cursor is outside the block.
  it('replaces the opening fence marker line with the copy button when cursor is outside', () => {
    const doc = 'text\n\n```\ncode here\n```\n\nend';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const openFrom = doc.indexOf('```');
    const openRep = reps.find(
      (d) => d.from === openFrom && d.to > openFrom && d.spec.widget &&
        String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'CopyButtonWidget',
    );
    expect(openRep).toBeDefined();
    // The replace must cover the whole "```" marker text, not just anchor a point widget.
    expect(openRep!.to).toBe(openFrom + 3);
  });

  it('replaces the closing fence marker line when cursor is outside', () => {
    const doc = 'text\n\n```\ncode here\n```\n\nend';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const closeFrom = doc.lastIndexOf('```');
    const closeRep = reps.find((d) => d.from === closeFrom && !d.spec.widget);
    expect(closeRep).toBeDefined();
  });

  it('does NOT replace fence marker lines when cursor is inside the block', () => {
    const doc = 'text\n\n```\ncode here\n```\n\nend';
    const codeStart = doc.indexOf('code here');
    const view = createView(doc, codeStart + 2);
    const reps = replaceDecorations(view);
    const openFrom = doc.indexOf('```');
    const closeFrom = doc.lastIndexOf('```');
    const fenceReps = reps.filter((d) => d.from === openFrom || d.from === closeFrom);
    expect(fenceReps).toHaveLength(0);
  });

  it('preserves the language on the copy button for ```lang fences', () => {
    const doc = 'text\n\n```python\ncode here\n```\n\nend';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const openFrom = doc.indexOf('```');
    const openRep = reps.find((d) => d.from === openFrom && d.spec.widget);
    expect(openRep).toBeDefined();
    expect((openRep!.spec.widget as { language?: string }).language).toBe('python');
  });
});

describe('task checkboxes', () => {
  it('replaces unchecked task with widget when cursor is outside', () => {
    const doc = '- [ ] task one\n\nother line';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const taskReps = reps.filter((d) => d.from <= 6 && d.spec.widget);
    expect(taskReps.length).toBeGreaterThan(0);
  });

  it('replaces asterisk task identically to dash task', () => {
    const dashView = createView('- [ ] task one\n\nother line', 25);
    const starView = createView('* [ ] task one\n\nother line', 25);
    const dashRep = replaceDecorations(dashView).filter((d) => d.spec.widget && d.from === 0);
    const starRep = replaceDecorations(starView).filter((d) => d.spec.widget && d.from === 0);
    expect(starRep).toHaveLength(1);
    expect(dashRep).toHaveLength(1);
    expect(starRep[0].to).toBe(dashRep[0].to);
  });

  // INVARIANT: an LLM-style multi-space task line (`*   [ ]`) must NOT be  Why: LLMs emit task markers with multiple spaces; mistaking that for a plain bullet mid-typing would flicker the checkbox, so build-structural tolerates the extra spaces.
  // mistaken for a plain bullet during the mid-typing reparse — the flicker
  // guard in build-structural.ts tolerates multiple spaces after the marker so
  // a BulletWidget is never swapped in (which leaves `[ ]` lingering as text).
  it('does not render a BulletWidget for a multi-space task (`*   [ ]`)', () => {
    const doc = '*   [ ] task one\n\nother line';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const widgetAtZero = reps.filter((d) => d.from === 0 && d.spec.widget);
    // Either no widget, or a widget that is NOT a BulletWidget.
    const bulletAtZero = widgetAtZero.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } })?.constructor?.name) === 'BulletWidget',
    );
    expect(bulletAtZero).toHaveLength(0);
    // The CheckboxWidget must be anchored at the bullet (from === 0) and cover the
    // whole `*   [ ]` marker — otherwise the bare `*` renders as a literal symbol.
    const checkboxAtZero = widgetAtZero.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } })?.constructor?.name) === 'CheckboxWidget',
    );
    expect(checkboxAtZero).toHaveLength(1);
    expect(checkboxAtZero[0].to).toBe(doc.indexOf(']') + 1);
  });

  it('replaces checked task and adds cm-task-done mark', () => {
    const doc = '- [x] done task\n\nother line';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const taskReps = reps.filter((d) => d.from <= 6 && d.spec.widget);
    expect(taskReps.length).toBeGreaterThan(0);
    const doneMarks = markDecorations(view, 'cm-task-done');
    expect(doneMarks.length).toBeGreaterThan(0);
  });

  it('keeps checkbox widget when cursor is on text after marker', () => {
    const view = createView('- [ ] task one\nother line', 8);
    const reps = replaceDecorations(view);
    const taskReps = reps.filter((d) => d.from <= 6 && d.spec.widget);
    expect(taskReps.length).toBeGreaterThan(0);
  });

  it('shows raw task syntax when cursor is on marker characters', () => {
    const view = createView('- [ ] task one\nother line', 2);
    const reps = replaceDecorations(view);
    const taskReps = reps.filter((d) => d.from <= 6 && d.spec.widget);
    expect(taskReps).toHaveLength(0);
  });

  // The CheckboxWidget Decoration.replace must end exactly at the `]` (= TaskMarker.to),
  // not consume the trailing space. Including the trailing space puts the cursor on
  // the right boundary of an atomic range at line-end and causes browser-specific
  // snapping into the widget on Tab/Cmd+]/click.
  it('replace decoration covers only `- [ ]` markup, not the trailing space', () => {
    // Doc `- [ ] task one\n...`: ListMark.from = 0, TaskMarker.to = 5 (after `]`).
    const doc = '- [ ] task one\nother line';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const taskReps = reps.filter((d) => d.spec.widget && d.from === 0);
    expect(taskReps).toHaveLength(1);
    expect(taskReps[0].to).toBe(5);
  });

  it('cm-task-done strikethrough starts after the trailing space, not on it', () => {
    // Doc `- [x] done task\n...`: TaskMarker.to = 5. Strike covers `done task` from pos 6.
    const doc = '- [x] done task\nother line';
    const view = createView(doc, doc.length - 1);
    const doneMarks = markDecorations(view, 'cm-task-done');
    expect(doneMarks.length).toBeGreaterThan(0);
    const firstMark = doneMarks.find((d) => d.from <= 10);
    expect(firstMark).toBeDefined();
    expect(firstMark!.from).toBe(6);
  });

  it('registers the checkbox widget range in EditorView.atomicRanges', () => {
    const doc = '- [ ] task one\nother line';
    const view = createView(doc, doc.length - 1);
    const providers = view.state.facet(EditorView.atomicRanges);
    let atomicCheckbox = false;
    for (const provider of providers) {
      const set = provider(view);
      set.between(0, view.state.doc.length, (from, to) => {
        if (from === 0 && to === 5) atomicCheckbox = true;
      });
    }
    expect(atomicCheckbox).toBe(true);
  });
});

describe('image with transcludeMap (ref-image entries)', () => {
  it('replaces ref: image with ImageWidget when URL is in map', () => {
    transcludeMap.set('abc123', { kind: 'ref-image', title: 'I', imageUrl: 'http://localhost/img.png' });
    const view = createView('text\n![alt](ref:abc123)\nend', 0);
    const reps = replaceDecorations(view);
    const imgReps = reps.filter((d) => d.spec.widget && d.from >= 5);
    expect(imgReps.length).toBeGreaterThan(0);
  });

  it('does not replace ref: image when URL is not in map', () => {
    const view = createView('text\n![alt](ref:unknown)\nend', 0);
    const reps = replaceDecorations(view);
    const imgWidgets = reps.filter(
      (d) => d.spec.widget && String(d.spec.widget.constructor?.name) === 'ImageWidget',
    );
    expect(imgWidgets).toHaveLength(0);
  });

  it('renders two images on the same line', () => {
    transcludeMap.set('a', { kind: 'ref-image', title: 'A', imageUrl: 'http://localhost/a.png' });
    transcludeMap.set('b', { kind: 'ref-image', title: 'B', imageUrl: 'http://localhost/b.png' });
    const view = createView('![x](ref:a) ![y](ref:b)\nend', 27);
    const reps = replaceDecorations(view);
    const imgWidgets = reps.filter(
      (d) => d.spec.widget && String(d.spec.widget.constructor?.name) === 'ImageWidget',
    );
    expect(imgWidgets).toHaveLength(2);
  });
});

// SYSTEM: transclusion — content embeds (ref-text + doc) reuse the Image node path.
describe('transclusion — content embeds', () => {
  it('replaces ![alt](ref:id) text-ref with TransclusionWidget', () => {
    transcludeMap.set('md1', { kind: 'ref-text', title: 'Note', content: '# Heading' });
    const view = createView('text\n![Note](ref:md1)\nend', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => d.spec.widget && String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets.length).toBe(1);
    const w = widgets[0].spec.widget as TransclusionWidget;
    expect(w.kind).toBe('ref');
    expect(w.entry.title).toBe('Note');
  });

  it('replaces ![alt](doc:id) with a doc TransclusionWidget', () => {
    transcludeMap.set('docA', { kind: 'doc', title: 'Page', content: 'body text' });
    const view = createView('see ![Page](doc:docA) here', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => d.spec.widget && String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets.length).toBe(1);
    expect((widgets[0].spec.widget as TransclusionWidget).kind).toBe('doc');
  });

  it('replaces ![alt](bare-id) doc transclusion', () => {
    transcludeMap.set('docB', { kind: 'doc', title: 'Bare', content: 'x' });
    const view = createView('see ![Bare](docB) end', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => d.spec.widget && String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets.length).toBe(1);
  });

  it('image refs still use ImageWidget (not TransclusionWidget)', () => {
    transcludeMap.set('img1', { kind: 'ref-image', title: 'I', imageUrl: 'http://localhost/img.png' });
    const doc = '![I](ref:img1) end';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const transclusionWidgets = reps.filter(
      (d) => d.spec.widget && String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(transclusionWidgets).toHaveLength(0);
    const imgWidgets = reps.filter(
      (d) => d.spec.widget && String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'ImageWidget',
    );
    expect(imgWidgets.length).toBe(1);
  });

  it('renders a broken doc id (not in transcludeMap) as a non-widget Image fallback', () => {
    // Unresolved doc id: no ImageWidget, no TransclusionWidget — falls through to
    // the generic Image link-mark hiding path (no silent stale content).
    const view = createView('see ![Broken](doc:nope) end', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter((d) => d.spec.widget);
    const transclusion = widgets.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    const images = widgets.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'ImageWidget',
    );
    expect(transclusion).toHaveLength(0);
    expect(images).toHaveLength(0);
  });
});

// SYSTEM: transclusion — standalone lines render as a CM6 block widget (gapless,
// no line-height strut). Inline embeds keep the inline widget.
describe('transclusion — block vs inline rendering', () => {
  it('standalone ![t](doc:id) line renders as a BLOCK widget spanning the whole line', () => {
    transcludeMap.set('blkDoc', { kind: 'doc', title: 'B', content: 'x' });
    const view = createView('before\n![B](doc:blkDoc)\nafter', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets.length).toBe(1);
    const w = widgets[0];
    expect(w.spec.block).toBe(true);
    // Whole-line range: from start of the standalone line to its end (no newline).
    const line = view.state.doc.lineAt(view.state.doc.toString().indexOf('![B]'));
    expect(w.from).toBe(line.from);
    expect(w.to).toBe(line.to);
  });

  it('standalone ![t](ref:id) text-ref line renders as a BLOCK widget', () => {
    transcludeMap.set('blkRef', { kind: 'ref-text', title: 'R', content: 'x' });
    const view = createView('lead\n![R](ref:blkRef)\ntrail', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets.length).toBe(1);
    expect(widgets[0].spec.block).toBe(true);
  });

  it('inline (mid-paragraph) transclusion keeps the INLINE widget', () => {
    transcludeMap.set('inlineDoc', { kind: 'doc', title: 'I', content: 'x' });
    const view = createView('see ![I](doc:inlineDoc) end', 0);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets.length).toBe(1);
    expect(widgets[0].spec.block).toBeFalsy();
  });

  it('at depth >= 1 the embed degrades to a link widget, not a nested band', () => {
    transcludeMap.set('nestedDoc', { kind: 'doc', title: 'N', content: 'x' });
    const state = EditorState.create({
      doc: 'before\n![N](doc:nestedDoc)\nafter',
      selection: { anchor: 0 },
      extensions: [
        markdown({ base: markdownLanguage }),
        livePreviewField, listLinePlugin, cursorLinePlugin,
        transclusionDepth.of(1),
      ],
    });
    const view = new EditorView({ state, parent: document.createElement('div') });
    ensureSyntaxTree(view.state, view.state.doc.length, 5000);
    view.dispatch({ selection: { anchor: 0 } });
    const reps = replaceDecorations(view);
    const names = reps.map((d) => String((d.spec.widget as { constructor?: { name?: string } })?.constructor?.name));
    expect(names).toContain('TransclusionLinkWidget');
    expect(names).not.toContain('TransclusionWidget');
  });

  it('block widget reveals the raw syntax when cursor enters its line', () => {
    transcludeMap.set('revealDoc', { kind: 'doc', title: 'R', content: 'x' });
    const doc = '![R](doc:revealDoc)';
    const cursorPos = 2; // inside the transclusion line
    const view = createView(doc, cursorPos);
    const reps = replaceDecorations(view);
    const widgets = reps.filter(
      (d) => String((d.spec.widget as { constructor?: { name?: string } }).constructor?.name) === 'TransclusionWidget',
    );
    expect(widgets).toHaveLength(0);
  });
});

describe('resolveTransclusion', () => {
  it('resolves a ref-text entry', () => {
    transcludeMap.set('t1', { kind: 'ref-text', title: 'T', content: 'c' });
    expect(resolveTransclusion('ref:t1')).toEqual({ kind: 'text', id: 't1' });
  });
  it('resolves a ref-image entry as image', () => {
    transcludeMap.set('i1', { kind: 'ref-image', title: 'I', imageUrl: 'u' });
    expect(resolveTransclusion('ref:i1')).toEqual({ kind: 'image', id: 'i1' });
  });
  it('resolves a doc: entry', () => {
    transcludeMap.set('d1', { kind: 'doc', title: 'D', content: 'c' });
    expect(resolveTransclusion('doc:d1')).toEqual({ kind: 'doc', id: 'd1' });
  });
  it('resolves a bare id that is a document', () => {
    transcludeMap.set('d2', { kind: 'doc', title: 'D', content: 'c' });
    expect(resolveTransclusion('d2')).toEqual({ kind: 'doc', id: 'd2' });
  });
  it('returns null for an http URL', () => {
    expect(resolveTransclusion('https://example.com/x.png')).toBeNull();
  });
  it('returns null for note: scheme', () => {
    expect(resolveTransclusion('note:thread1')).toBeNull();
  });
  it('returns null for an unknown ref id', () => {
    expect(resolveTransclusion('ref:missing')).toBeNull();
  });
  it('returns null for an unknown bare id', () => {
    expect(resolveTransclusion('unknown-bare-id')).toBeNull();
  });
});

describe('bullet list markers', () => {
  it('replaces dash bullet with BulletWidget when cursor is outside', () => {
    const doc = '- item one\n\nother line';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const bulletReps = reps.filter((d) => d.from === 0 && d.spec.widget);
    expect(bulletReps.length).toBeGreaterThan(0);
  });

  it('replaces asterisk bullet with BulletWidget when cursor is outside', () => {
    const doc = '* item one\n\nother line';
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    const bulletReps = reps.filter((d) => d.from === 0 && d.spec.widget);
    expect(bulletReps.length).toBeGreaterThan(0);
  });

  it('keeps bullet widget when cursor is on text after marker', () => {
    const view = createView('- item one\nother line', 4);
    const reps = replaceDecorations(view);
    const bulletReps = reps.filter((d) => d.from === 0 && d.spec.widget);
    expect(bulletReps.length).toBeGreaterThan(0);
  });

  it('shows raw dash bullet when cursor is on marker characters', () => {
    const view = createView('- item one\nother line', 1);
    const reps = replaceDecorations(view);
    const bulletReps = reps.filter((d) => d.from === 0 && d.spec.widget);
    expect(bulletReps).toHaveLength(0);
  });
});

describe('list line hanging indent', () => {
  it('adds cm-list-line decoration to bullet list items', () => {
    const doc = '- item one\n\nother line';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    expect(listLines.length).toBeGreaterThan(0);
  });

  it('sets indent for regular bullet items', () => {
    const doc = '- item one\n\nother line';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    const firstLine = listLines.find((d) => d.from === 0);
    expect(firstLine).toBeDefined();
    expect((firstLine!.spec as Record<string, unknown>).attributes).toHaveProperty('style');
    const style = (firstLine!.spec as Record<string, unknown>).attributes as Record<string, string>;
    const indent = LIST_GUTTER;
    expect(style.style).toContain(`padding-left:${indent}em`);
    expect(style.style).toContain(`text-indent:${-indent + BASE_PADDING}em`);
  });
});

describe('list decorations persist with cursor inside', () => {
  it('keeps cm-list-line when cursor is inside ListItem', () => {
    const doc = '- item one\n- item two\n\nother line';
    const view = createView(doc, 5);
    const listLines = lineDecorations(view, 'cm-list-line');
    expect(listLines.length).toBeGreaterThan(0);
  });

  it('keeps leading whitespace hidden when cursor is inside ListItem', () => {
    const doc = '- l1\n  - l2\n\nend';
    const view = createView(doc, 5);
    const reps = replaceDecorations(view);
    const line2 = view.state.doc.line(2);
    const leadingRep = reps.find((d) => d.from === line2.from && d.to > line2.from);
    expect(leadingRep).toBeDefined();
  });

  it('keeps bullet widget on sibling items when cursor is on one', () => {
    const doc = '- item one\n- item two\n\nother line';
    const view = createView(doc, 1);
    const reps = replaceDecorations(view);
    const bulletReps = reps.filter((d) => d.spec.widget);
    const line2From = view.state.doc.line(2).from;
    const secondBullet = bulletReps.find((d) => d.from === line2From);
    expect(secondBullet).toBeDefined();
  });
});

describe('multi-level list indent — bullets', () => {
  const doc = '- l1\n  - l2\n    - l3\n      - l4\n        - l5\n\nend';
  const expectedIndents = [1, 2, 3, 4, 5].map(l => (l - 1) * LIST_STEP + LIST_GUTTER);

  it('sets uniform indent per nesting level', () => {
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    for (let i = 0; i < expectedIndents.length; i++) {
      const lineFrom = view.state.doc.line(i + 1).from;
      const dec = listLines.find((d) => d.from === lineFrom);
      expect(dec, `missing cm-list-line for level ${i + 1} at pos ${lineFrom}`).toBeDefined();
      const style = ((dec!.spec as Record<string, unknown>).attributes as Record<string, string>).style;
      expect(style, `level ${i + 1} indent`).toContain(`padding-left:${expectedIndents[i]}em`);
      // WHY: text-indent is CONSTANT across nesting levels so the bullet steps  Why: padding-left grows per level to indent the bullet; if text-indent also changed per level it would cancel the padding and collapse every bullet to BASE_PADDING (flat lists).
      // right per level (padding grows, text-indent does not cancel it). A per-level
      // text-indent collapsed every bullet to BASE_PADDING → flat space-indented lists.
      expect(style, `level ${i + 1} textIndent`).toContain(`text-indent:${-(LIST_GUTTER - BASE_PADDING)}em`);
    }
  });

  it('hides leading whitespace on nested lines', () => {
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    for (let i = 1; i < expectedIndents.length; i++) {
      const line = view.state.doc.line(i + 1);
      const leadingRep = reps.find((d) => d.from === line.from && d.to > line.from);
      expect(leadingRep, `missing leading-space replacement on line ${i + 1}`).toBeDefined();
    }
  });
});

describe('tab-indented list leading whitespace', () => {
  // Tabs must be hidden like spaces; otherwise the rendered tab adds to the stepped
  // padding and double-indents tab-authored lists.
  const doc = '- l1\n\t- l2\n\t\t- l3\n\nend';

  it('hides leading tabs on nested lines', () => {
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    for (const n of [2, 3]) {
      const line = view.state.doc.line(n);
      const leadingRep = reps.find((d) => d.from === line.from && d.to > line.from);
      expect(leadingRep, `missing leading-tab replacement on line ${n}`).toBeDefined();
    }
  });

  it('applies cm-list-line to tab-indented nested items', () => {
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    for (const n of [1, 2, 3]) {
      const lineFrom = view.state.doc.line(n).from;
      expect(listLines.find((d) => d.from === lineFrom), `missing cm-list-line on line ${n}`).toBeDefined();
    }
  });
});

describe('multi-level list indent — checkboxes', () => {
  const doc = '- [ ] l1\n  - [ ] l2\n    - [ ] l3\n      - [ ] l4\n        - [ ] l5\n\nend';
  const expectedIndents = [1, 2, 3, 4, 5].map(l => (l - 1) * LIST_STEP + LIST_GUTTER);

  it('sets uniform indent per nesting level', () => {
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    for (let i = 0; i < expectedIndents.length; i++) {
      const lineFrom = view.state.doc.line(i + 1).from;
      const dec = listLines.find((d) => d.from === lineFrom);
      expect(dec, `missing cm-list-line for level ${i + 1} at pos ${lineFrom}`).toBeDefined();
      const style = ((dec!.spec as Record<string, unknown>).attributes as Record<string, string>).style;
      expect(style, `level ${i + 1} indent`).toContain(`padding-left:${expectedIndents[i]}em`);
      // WHY: text-indent is CONSTANT across nesting levels so the bullet steps  Why: padding-left grows per level to indent the bullet; if text-indent also changed per level it would cancel the padding and collapse every bullet to BASE_PADDING (flat lists).
      // right per level (padding grows, text-indent does not cancel it). A per-level
      // text-indent collapsed every bullet to BASE_PADDING → flat space-indented lists.
      expect(style, `level ${i + 1} textIndent`).toContain(`text-indent:${-(LIST_GUTTER - BASE_PADDING)}em`);
    }
  });

  it('hides leading whitespace on nested checkbox lines', () => {
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    for (let i = 1; i < expectedIndents.length; i++) {
      const line = view.state.doc.line(i + 1);
      const leadingRep = reps.find((d) => d.from === line.from && d.to > line.from);
      expect(leadingRep, `missing leading-space replacement on line ${i + 1}`).toBeDefined();
    }
  });
});

describe('multi-level list indent — numbered', () => {
  const doc = '1. l1\n   1. l2\n      1. l3\n         1. l4\n            1. l5\n\nend';
  const expectedIndents = [1, 2, 3, 4, 5].map(l => (l - 1) * LIST_STEP + LIST_GUTTER);

  it('sets uniform indent per nesting level', () => {
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    for (let i = 0; i < expectedIndents.length; i++) {
      const lineFrom = view.state.doc.line(i + 1).from;
      const dec = listLines.find((d) => d.from === lineFrom);
      expect(dec, `missing cm-list-line for level ${i + 1} at pos ${lineFrom}`).toBeDefined();
      const style = ((dec!.spec as Record<string, unknown>).attributes as Record<string, string>).style;
      expect(style, `level ${i + 1} indent`).toContain(`padding-left:${expectedIndents[i]}em`);
      // WHY: text-indent is CONSTANT across nesting levels so the bullet steps  Why: padding-left grows per level to indent the bullet; if text-indent also changed per level it would cancel the padding and collapse every bullet to BASE_PADDING (flat lists).
      // right per level (padding grows, text-indent does not cancel it). A per-level
      // text-indent collapsed every bullet to BASE_PADDING → flat space-indented lists.
      expect(style, `level ${i + 1} textIndent`).toContain(`text-indent:${-(LIST_GUTTER - BASE_PADDING)}em`);
    }
  });

  it('hides leading whitespace on nested numbered lines', () => {
    const view = createView(doc, doc.length - 1);
    const reps = replaceDecorations(view);
    for (let i = 1; i < expectedIndents.length; i++) {
      const line = view.state.doc.line(i + 1);
      const leadingRep = reps.find((d) => d.from === line.from && d.to > line.from);
      expect(leadingRep, `missing leading-space replacement on line ${i + 1}`).toBeDefined();
    }
  });
});

describe('parseSizeFromAlt', () => {
  it('returns empty for alt without pipe', () => {
    expect(parseSizeFromAlt('my image')).toEqual({});
  });

  it('parses width in pixels', () => {
    expect(parseSizeFromAlt('alt|300')).toEqual({ width: '300px', height: undefined });
  });

  it('parses width in percent', () => {
    expect(parseSizeFromAlt('alt|30%')).toEqual({ width: '30%', height: undefined });
  });

  it('parses width and height', () => {
    expect(parseSizeFromAlt('alt|300x200')).toEqual({ width: '300px', height: '200px' });
  });

  it('parses percent width with pixel height', () => {
    expect(parseSizeFromAlt('alt|50%x300')).toEqual({ width: '50%', height: '300px' });
  });

  it('returns empty for invalid size after pipe', () => {
    expect(parseSizeFromAlt('alt|invalid')).toEqual({});
  });

  it('uses last pipe when alt contains multiple pipes', () => {
    expect(parseSizeFromAlt('alt|text|300')).toEqual({ width: '300px', height: undefined });
  });
});

describe('lazy continuation lines — style inheritance fix', () => {
  it('does NOT apply cm-list-line to lazy continuation (no indent)', () => {
    const doc = '- bullet one\nlazy continuation';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    expect(listLines.length).toBe(1);
    expect(listLines[0].from).toBe(0);
  });

  it('applies cm-list-line to properly indented continuation', () => {
    const doc = '- bullet one\n  proper continuation';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    expect(listLines.length).toBe(2);
  });

  it('applies cm-list-line to first line even with lazy continuation after', () => {
    const doc = '- bullet one\nlazy';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    const firstLineDec = listLines.find((d) => d.from === 0);
    expect(firstLineDec).toBeDefined();
  });

  it('does NOT apply cm-list-line to lazy continuation after nested bullet', () => {
    const doc = '- bullet\n  - nested\nlazy line';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    const lazyLineFrom = view.state.doc.line(3).from;
    const lazyDec = listLines.find((d) => d.from === lazyLineFrom);
    expect(lazyDec).toBeUndefined();
  });

  it('applies cm-list-line to all proper lines in multi-line bullet', () => {
    const doc = '- line1\n  line2\n  line3';
    const view = createView(doc, doc.length - 1);
    const listLines = lineDecorations(view, 'cm-list-line');
    expect(listLines.length).toBe(3);
  });

  it('does NOT apply cm-task-done to lazy continuation after checked checkbox', () => {
    const doc = '- [x] done task\nlazy paragraph';
    const view = createView(doc, doc.length - 1);
    const taskDoneDecs = markDecorations(view, 'cm-task-done');
    expect(taskDoneDecs.length).toBe(1);
    const lazyLineFrom = view.state.doc.line(2).from;
    expect(taskDoneDecs[0].to).toBeLessThanOrEqual(lazyLineFrom);
  });

  it('does NOT apply cm-task-done to nested list under checked checkbox', () => {
    const doc = '- [x] done\n  - nested item';
    const view = createView(doc, doc.length - 1);
    const taskDoneDecs = markDecorations(view, 'cm-task-done');
    const nestedLineFrom = view.state.doc.line(2).from;
    const coveringDec = taskDoneDecs.find((d) => d.to > nestedLineFrom);
    expect(coveringDec).toBeUndefined();
  });
});

describe('color labels in inline code', () => {
  it('pure color cursor outside → mark present, replace hides color code', () => {
    const doc = 'see `#8ab4f8` here';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);
    const attrs = (markDecs[0].spec as Record<string, unknown>).attributes as Record<string, string> | undefined;
    expect(attrs).toBeDefined();
    expect(attrs!.style).toContain('background-color:#8ab4f8');

    const reps = replaceDecorations(view);
    const nonWidgetReps = reps.filter((d) => !d.spec.widget && d.from > doc.indexOf('`'));
    expect(nonWidgetReps.length).toBeGreaterThan(0);
  });

  it('pure color cursor inside → mark present, no replace', () => {
    const doc = 'see `#8ab4f8` here';
    const codeStart = doc.indexOf('`') + 1;
    const view = createView(doc, codeStart + 2);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);

    const reps = replaceDecorations(view);
    const nonWidgetReps = reps.filter((d) => !d.spec.widget && d.from > doc.indexOf('`'));
    expect(nonWidgetReps).toHaveLength(0);
  });

  it('color + label cursor outside → cm-color-swatch mark + replace hides prefix', () => {
    const doc = 'see `#8ab4f8 Draft` here';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);
    const attrs = (markDecs[0].spec as Record<string, unknown>).attributes as Record<string, string> | undefined;
    expect(attrs).toBeDefined();
    expect(attrs!.style).toContain('background-color:#8ab4f8');

    const reps = replaceDecorations(view);
    const nonWidgetReps = reps.filter((d) => !d.spec.widget && d.from > doc.indexOf('`'));
    expect(nonWidgetReps.length).toBeGreaterThan(0);
  });

  it('color + label cursor inside → cm-color-swatch mark, NO replace for prefix', () => {
    const doc = 'see `#8ab4f8 Draft` here';
    const codeStart = doc.indexOf('`') + 1;
    const view = createView(doc, codeStart + 5);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);

    const reps = replaceDecorations(view);
    const nonWidgetReps = reps.filter((d) => !d.spec.widget && d.from > doc.indexOf('`'));
    expect(nonWidgetReps).toHaveLength(0);
  });

  it('short hex #f00 with label → mark with bg-color:#f00', () => {
    const doc = 'see `#f00 Urgent` here';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);
    const attrs = (markDecs[0].spec as Record<string, unknown>).attributes as Record<string, string> | undefined;
    expect(attrs!.style).toContain('background-color:#f00');
  });

  it('does NOT apply cm-color-swatch for non-color inline code', () => {
    const doc = 'see `not a color` here';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs).toHaveLength(0);
  });

  it('does NOT apply cm-color-swatch for inline code with text around color', () => {
    const doc = 'see `color: #8ab4f8` here';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs).toHaveLength(0);
  });

  it('3-digit color with label works', () => {
    const doc = '`#1a2 text`';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);
  });

  it('8-digit color with label works', () => {
    const doc = '`#1a2b3c4d text`';
    const view = createView(doc, doc.length - 1);
    const markDecs = markDecorations(view, 'cm-color-swatch');
    expect(markDecs.length).toBeGreaterThan(0);
  });
});

// Regression: a table whose header row starts with a leading space must have its
// block replace span the WHOLE first line (from line start), not from the Lezer
// Table node (which begins after the space). Otherwise the leading space renders as
// an orphan 1-char `.cm-line`, which CM6 measureTextSize() samples → charWidth
// collapses to space-width → contentHeight craters below the real DOM → scroll-jump.
// Reproduced 2026-06-01 (leading space before table). See lessons/2026-05-30.
describe('table block replace covers leading whitespace (charWidth scroll-jump guard)', () => {
  function createTableView(doc: string, cursor: number): EditorView {
    const state = EditorState.create({
      doc,
      selection: { anchor: cursor },
      extensions: [markdown({ base: markdownLanguage }), tableRenderField],
    });
    const view = new EditorView({ state, parent: document.createElement('div') });
    ensureSyntaxTree(view.state, view.state.doc.length, 5000);
    view.dispatch({ selection: { anchor: cursor } });
    return view;
  }

  function tableReplaceRanges(view: EditorView) {
    const out: { from: number; to: number }[] = [];
    view.state.field(tableRenderField).decs.between(0, view.state.doc.length, (from, to) => {
      out.push({ from, to });
    });
    return out;
  }

  it('replace starts at line start when the header row has a leading space (cursor outside)', () => {
    const doc = ' **A** | **B** |\n| --- | --- |\n x | y |\n';
    const view = createTableView(doc, doc.length); // cursor after table → table rendered
    const reps = tableReplaceRanges(view);
    expect(reps.length).toBe(1);
    expect(reps[0].from).toBe(0); // line start, NOT 1 (after the leading space)
  });

  it('replace still starts at line start for a normal (no leading space) table', () => {
    const doc = '**A** | **B** |\n| --- | --- |\nx | y |\n';
    const view = createTableView(doc, doc.length);
    const reps = tableReplaceRanges(view);
    expect(reps.length).toBe(1);
    expect(reps[0].from).toBe(0);
  });
});

// SYSTEM: live-preview — first-line rendering in a nested (read-only) transclusion view.
// The nested view defaults its selection to position 0; reveal-on-cursor must be OFF so
// the first markdown element (mermaid, table, $$ math, bold) renders instead of raw.
// Driven by the revealAtCursor facet (mirrors nestedRenderExtensions wiring).
describe('nested view (revealAtCursor=false) — first element renders, not raw', () => {
  function createNestedView(
    doc: string,
    extensions: Extension[],
  ): EditorView {
    const state = EditorState.create({
      doc,
      selection: { anchor: 0 },
      extensions: [
        markdown({ base: markdownLanguage }),
        ...extensions,
        revealAtCursor.of(false),
      ],
    });
    const view = new EditorView({ state, parent: document.createElement('div') });
    ensureSyntaxTree(view.state, view.state.doc.length, 5000);
    view.dispatch({ selection: { anchor: 0 } });
    return view;
  }

  function fieldReplaceRanges(
    view: EditorView,
    field: StateField<{ decs: DecorationSet }>,
  ): { from: number; to: number; widget?: string }[] {
    const out: { from: number; to: number; widget?: string }[] = [];
    view.state.field(field).decs.between(0, view.state.doc.length, (from, to, dec) => {
      const widget = (dec as unknown as { spec: { widget?: { constructor?: { name?: string } } } })
        .spec.widget?.constructor?.name;
      out.push({ from, to, widget });
    });
    return out;
  }

  it('renders a leading ```mermaid block (not raw) with cursor at 0', () => {
    const doc = '```mermaid\ngraph TD; A-->B\n```';
    const view = createNestedView(doc, [mermaidBlockRenderField]);
    const reps = fieldReplaceRanges(view, mermaidBlockRenderField);
    const mermaid = reps.filter((d) => d.widget === 'MermaidWidget');
    expect(mermaid.length).toBe(1);
  });

  it('renders a leading table (not raw) with cursor at 0', () => {
    const doc = '**A** | **B**\n| --- | --- |\nx | y';
    const view = createNestedView(doc, [tableRenderField]);
    const reps = fieldReplaceRanges(view, tableRenderField);
    const table = reps.filter((d) => d.widget === 'TableWidget');
    expect(table.length).toBe(1);
  });

  it('renders a leading $$ math block (not raw) with cursor at 0', () => {
    const doc = '$$\nx^2 + y^2\n$$';
    const view = createNestedView(doc, [mathBlockRenderField]);
    const reps = fieldReplaceRanges(view, mathBlockRenderField);
    const math = reps.filter((d) => d.widget === 'BlockMathWidget');
    expect(math.length).toBe(1);
  });

  it('renders a leading bold span (hides EmphasisMark, not raw) with cursor at 0', () => {
    const doc = '**bold text**';
    const view = createNestedView(doc, [livePreviewField, listLinePlugin, cursorLinePlugin]);
    const reps = replaceDecorations(view);
    // EmphasisMark `**` positions should be hidden (replaced) within the bold range.
    const inBold = reps.filter((d) => d.from >= 0 && d.to <= doc.length);
    expect(inBold.length).toBeGreaterThan(0);
  });
});

// Regression: with the default facet (true) reveal-on-cursor stays active — cursor on the
// element still reveals raw markdown (editing affordance preserved in the root editor).
describe('root view (revealAtCursor default true) — cursor reveals raw', () => {
  function createRootView(doc: string, cursor: number, extensions: Extension[]): EditorView {
    const state = EditorState.create({
      doc,
      selection: { anchor: cursor },
      extensions: [
        markdown({ base: markdownLanguage }),
        ...extensions,
        // NO revealAtCursor override → default true (root editor).
      ],
    });
    const view = new EditorView({ state, parent: document.createElement('div') });
    ensureSyntaxTree(view.state, view.state.doc.length, 5000);
    view.dispatch({ selection: { anchor: cursor } });
    return view;
  }

  it('cursor inside a leading mermaid block → no rendered widget (raw revealed)', () => {
    const doc = '```mermaid\ngraph TD; A-->B\n```';
    const cursor = doc.indexOf('graph');
    const view = createRootView(doc, cursor, [mermaidBlockRenderField]);
    const reps: { from: number; to: number; widget?: string }[] = [];
    view.state.field(mermaidBlockRenderField).decs.between(0, view.state.doc.length, (from, to, dec) => {
      const widget = (dec as unknown as { spec: { widget?: { constructor?: { name?: string } } } })
        .spec.widget?.constructor?.name;
      reps.push({ from, to, widget });
    });
    const mermaid = reps.filter((d) => d.widget === 'MermaidWidget');
    expect(mermaid).toHaveLength(0);
  });

  it('cursor inside a leading bold span → EmphasisMark visible (raw revealed)', () => {
    const doc = '**bold text**';
    const cursor = 4;
    const view = createRootView(doc, cursor, [livePreviewField, listLinePlugin, cursorLinePlugin]);
    const reps = replaceDecorations(view);
    const inBold = reps.filter((d) => d.from >= 0 && d.to <= doc.length);
    expect(inBold).toHaveLength(0);
  });
});
