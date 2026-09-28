/**
 * Unit tests for buildStructuralDecorations — the structural StateField builder.
 *
 * Pure input→output over the REAL markdown syntax tree (no view mount): each
 * test builds an EditorState, forces the parse, and walks the DecorationSet the
 * builder returns. The contracts pinned here are the historically-defective
 * ones (task-marker boundaries, the `* [ ]` flicker guard, fence-line hiding,
 * link classification attrs, the whitespace-line scroll-jump guard) — see the
 * INLINE Why/INVARIANT comments in build-structural.ts for each rule's story.
 */
// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { buildStructuralDecorations } from './build-structural';
import { transcludeMap } from './effects';
import { validDocIds, validNoteThreadIds } from './link-validity';

interface Dec {
  from: number;
  to: number;
  cls?: string;
  attrs?: Record<string, string>;
  widget?: { constructor: { name: string } };
  block?: boolean;
}

function decsFor(doc: string, cursor = -1): Dec[] {
  const state = EditorState.create({
    doc,
    selection: { anchor: cursor < 0 ? doc.length : cursor },
    extensions: [markdown({ base: markdownLanguage })],
  });
  ensureSyntaxTree(state, state.doc.length, 5000);
  const set = buildStructuralDecorations(state);
  const out: Dec[] = [];
  set.between(0, state.doc.length, (from, to, dec) => {
    const spec = (dec as unknown as { spec: Record<string, unknown> }).spec;
    out.push({
      from,
      to,
      cls: typeof spec.class === 'string' ? spec.class : undefined,
      attrs: spec.attributes as Record<string, string> | undefined,
      widget: spec.widget as { constructor: { name: string } } | undefined,
      block: spec.block === true,
    });
  });
  return out;
}

// ── Task markers ─────────────────────────────────────────────────────────────

describe('task markers', () => {
  it('a checked task hides bullet+[x] (NOT the trailing space) and strikes the content', () => {
    const doc = '- [x] done';
    const decs = decsFor(doc);
    const checkbox = decs.find((d) => d.widget?.constructor.name === 'CheckboxWidget');
    expect(checkbox, 'CheckboxWidget replace must exist').toBeDefined();
    // Replace-range INVARIANT (build-structural.ts) — covers ONLY `- [x]` (0..5), never the trailing space.
    expect(checkbox!.from).toBe(0);
    expect(checkbox!.to).toBe(5);
    const strike = decs.find((d) => d.cls === 'cm-task-done');
    expect(strike, 'checked task content must be struck').toBeDefined();
    expect(doc.slice(strike!.from, strike!.to)).toBe('done');
  });

  it('an unchecked task gets a checkbox but NO strikethrough', () => {
    const decs = decsFor('- [ ] todo');
    expect(decs.some((d) => d.widget?.constructor.name === 'CheckboxWidget')).toBe(true);
    expect(decs.some((d) => d.cls === 'cm-task-done')).toBe(false);
  });

  it('the `* [ ]` flicker guard: a star bullet on a task line never swaps in a BulletWidget', () => {
    // Mid-reparse the TaskMarker node is missing briefly; the guard at the
    // ListMark branch must keep `*` from becoming a bullet next to a `[ ]`.
    const decs = decsFor('* [ ] maybe task');
    expect(decs.some((d) => d.widget?.constructor.name === 'BulletWidget')).toBe(false);
  });

  it('cursor inside the task markup reveals it (no checkbox replace)', () => {
    const doc = '- [ ] todo';
    const decs = decsFor(doc, 3);
    expect(decs.some((d) => d.widget?.constructor.name === 'CheckboxWidget')).toBe(false);
  });
});

// ── Bullets ──────────────────────────────────────────────────────────────────

describe('list bullets', () => {
  it('a plain bullet is replaced by a BulletWidget including its trailing spaces', () => {
    const doc = '-   item';
    const decs = decsFor(doc);
    const bullet = decs.find((d) => d.widget?.constructor.name === 'BulletWidget');
    expect(bullet, 'BulletWidget must exist').toBeDefined();
    expect(doc.slice(bullet!.from, bullet!.to)).toBe('-   ');
  });
});

// ── Hidden markers (reveal-on-cursor) ────────────────────────────────────────

describe('hidden markers', () => {
  it('cursor outside: the HeaderMark AND one following space are hidden', () => {
    const doc = 'intro\n# Title';
    const decs = decsFor(doc, 0);
    const hidden = decs.filter((d) => !d.widget && !d.cls);
    expect(hidden.some((d) => doc.slice(d.from, d.to) === '# ')).toBe(true);
  });

  it('cursor inside the header keeps the markup visible', () => {
    const doc = 'intro\n# Title';
    const decs = decsFor(doc, doc.indexOf('#') + 1);
    expect(decs.some((d) => doc.slice(d.from, d.to) === '# ')).toBe(false);
  });

  it('emphasis marks are hidden only when the cursor is outside the emphasis node', () => {
    const doc = 'a **bold** b';
    expect(decsFor(doc).some((d) => doc.slice(d.from, d.to) === '**')).toBe(true);
    expect(decsFor(doc, 5).some((d) => doc.slice(d.from, d.to) === '**')).toBe(false);
  });
});

// ── Fenced code ──────────────────────────────────────────────────────────────

describe('fenced code', () => {
  const doc = '```ts\nconst a = 1;\n```\nafter';

  it('cursor outside: the opening fence line becomes a CopyButtonWidget replace', () => {
    const decs = decsFor(doc);
    const copy = decs.find((d) => d.widget?.constructor.name === 'CopyButtonWidget');
    expect(copy, 'CopyButtonWidget on the opening fence').toBeDefined();
    expect(doc.slice(copy!.from, copy!.to)).toBe('```ts');
    // The closing fence line is replaced with nothing (no widget).
    const closing = decs.find((d) => !d.widget && !d.cls && doc.slice(d.from, d.to).startsWith('```') && d.from > copy!.from);
    expect(closing, 'closing fence hidden').toBeDefined();
    // Code text itself is untouched.
    expect(decs.some((d) => doc.slice(d.from, d.to) === 'const a = 1;')).toBe(false);
  });

  it('cursor inside the block keeps BOTH fence lines visible (edit affordance)', () => {
    const decs = decsFor(doc, doc.indexOf('const'));
    expect(decs.some((d) => d.widget?.constructor.name === 'CopyButtonWidget')).toBe(false);
    expect(decs.filter((d) => !d.widget && !d.cls && doc.slice(d.from, d.to).startsWith('```')).length).toBe(0);
  });
});

// ── Inline code color swatch ─────────────────────────────────────────────────

describe('inline code color swatch', () => {
  it('`#f00 label` marks a swatch and hides the color token', () => {
    const doc = 'color `#ff0000 red` end';
    const decs = decsFor(doc);
    const swatch = decs.find((d) => d.cls === 'cm-color-swatch');
    expect(swatch, 'swatch mark over the whole inline code').toBeDefined();
    expect(swatch!.attrs?.['style']).toBe('background-color:#ff0000');
    const hiddenColor = decs.find((d) => !d.widget && !d.cls && doc.slice(d.from, d.to).replace(/\s.*/, '') === '#ff0000');
    expect(hiddenColor, 'the color token itself is hidden').toBeDefined();
    expect(doc.slice(hiddenColor!.from, hiddenColor!.to)).toBe('#ff0000 ');
  });
});

// ── Links ────────────────────────────────────────────────────────────────────

describe('links', () => {
  it('note: links carry the note class, data attrs, and hidden brackets', () => {
    validNoteThreadIds.add('t1');
    const doc = 'see [note link](note:t1) here';
    const decs = decsFor(doc);
    const mark = decs.find((d) => d.cls === 'cm-note-link');
    expect(mark, 'cm-note-link mark').toBeDefined();
    expect(mark!.attrs?.['data-link-type']).toBe('note');
    expect(mark!.attrs?.['data-link-id']).toBe('note:t1');
    expect(doc.slice(mark!.from, mark!.to)).toBe('note link');
    // Both bracket runs hidden.
    expect(decs.some((d) => doc.slice(d.from, d.to) === '[')).toBe(true);
    expect(decs.some((d) => doc.slice(d.from, d.to).startsWith('](note:t1)'))).toBe(true);
  });

  it('bare-id links classify as doc; # anchors and mailto stay plain cm-link', () => {
    validDocIds.add('abc-123');
    const docDecs = decsFor('go [doc](abc-123) now');
    expect(docDecs.find((d) => d.cls === 'cm-doc-link')?.attrs?.['data-link-type']).toBe('doc');
    const anchor = decsFor('go [sec](#top) now');
    expect(anchor.some((d) => d.cls === 'cm-doc-link')).toBe(false);
    expect(anchor.some((d) => d.cls === 'cm-link')).toBe(true);
    const mail = decsFor('go [m](mailto:a@b.c) now');
    expect(mail.some((d) => d.cls === 'cm-doc-link')).toBe(false);
    expect(mail.some((d) => d.cls === 'cm-ext-link')).toBe(false);
  });

  it('http links classify as external', () => {
    const decs = decsFor('go [ext](https://x.dev) now');
    expect(decs.find((d) => d.cls === 'cm-ext-link')?.attrs?.['data-link-type']).toBe('ext');
  });
});

// ── Whitespace-line scroll-jump guard ────────────────────────────────────────

describe('whitespace-line guard', () => {
  it('a short whitespace-only line is hidden OUTSIDE code, kept INSIDE fenced code', () => {
    const plain = decsFor('a\n   \nb');
    expect(plain.some((d) => !d.widget && !d.cls && d.to - d.from === 3)).toBe(true);
    const withCode = decsFor('```\n   \n```');
    const wsLine = withCode.find((d) => !d.widget && !d.cls && d.to - d.from === 3);
    expect(wsLine, 'whitespace line inside code must NOT be hidden').toBeUndefined();
  });
});

// ── Transclusion standalone-line block widget ────────────────────────────────

describe('transclusion', () => {
  it('a standalone embed line renders as a BLOCK replace over the whole line', () => {
    transcludeMap.set('ref-1', {
      kind: 'ref-text', title: 'R', content: 'body',
    });
    try {
      const doc = 'before\n![embed](ref:ref-1)\nafter';
      const decs = decsFor(doc);
      const block = decs.find((d) => d.block);
      expect(block, 'standalone embed must be a block replace').toBeDefined();
      expect(block!.widget?.constructor.name).toBe('TransclusionWidget');
      expect(doc.slice(block!.from, block!.to)).toBe('![embed](ref:ref-1)');
      // Inline (mid-sentence) embed stays a non-block widget.
      const inline = decsFor('x ![embed](ref:ref-1) y');
      const t = inline.find((d) => d.widget?.constructor.name === 'TransclusionWidget');
      expect(t, 'inline embed keeps its widget').toBeDefined();
      expect(t!.block).toBeFalsy();
    } finally {
      transcludeMap.delete('ref-1');
    }
  });
});

// ── Inline math ──────────────────────────────────────────────────────────────

describe('inline math', () => {
  it('$x^2$ becomes an InlineMathWidget; single-line $$display$$ does not', () => {
    const doc = 'eq $x^2$ end';
    const decs = decsFor(doc);
    const m = decs.find((d) => d.widget?.constructor.name === 'InlineMathWidget');
    expect(m, 'inline math widget').toBeDefined();
    expect(doc.slice(m!.from, m!.to)).toBe('$x^2$');
    const display = decsFor('$$x^2$$');
    expect(display.some((d) => d.widget?.constructor.name === 'InlineMathWidget')).toBe(false);
  });
});
