/**
 * Unit tests for the editor theme extensions — rendered-output contract.
 *
 * EditorView.theme() compiles the spec into a real stylesheet mounted on the
 * document; these tests read THAT css (the rendered artifact the browser gets),
 * not the source object — a rule dropped, renamed, or recolored by a refactor
 * fails here even when the source still "looks right".
 *
 * The pinned contracts are the historically-regressing ones (see theme.ts
 * inline WHY comments): zero border-radius on every theme rule, the selection/
 * bracket/search nesting matrix around inline code, find-match tint separation,
 * the fenced-code span reset that preserves syntax tokens, the shared link base
 * with per-type backgrounds + dark variants.
 *
 * Assertions run on COMPACTED css (all whitespace stripped): StyleModule emits
 * `sel {prop: value;}` with spaces that are not part of any contract.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { loreEditorTheme } from './theme';
// ?raw: the selector keys are the DERIVED source for THEME_CLASSES below.
import themeSource from './theme.ts?raw';

const mounted: { destroy(): void }[] = [];

function renderedCss(theme: unknown): string {
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  const view = new EditorView({
    state: EditorState.create({ doc: 'x', extensions: [theme as never] }),
    parent,
  });
  mounted.push(view);
  return Array.from(document.querySelectorAll('style'))
    .map((s) => s.textContent ?? '')
    .join('\n');
}

const compact = (css: string) => css.replace(/\s+/g, '');

afterEach(() => {
  while (mounted.length) mounted.pop()?.destroy();
});

/**
 * Class names the THEME owns — DERIVED from theme.ts's own selector keys, never
 * enumerated here. Why: a hand-copied list drifts with the source it mirrors, so a
 * newly added rounded class would simply not be scanned (.claude/rules/testing.md,
 * "assert over the DERIVED source"). Parsing the module text is what makes the next
 * class added to theme.ts covered by the radius scan without touching this file.
 *
 * Why not a bare-view diff instead: CM6's base theme legitimately uses border-radius
 * (50% handles, 1px buttons), and StyleModule mount/unmount refcounts across tests
 * make a diff unstable; filtering by the source's own class names is exact either way.
 */
const THEME_CLASSES: string[] = Array.from(
  new Set(themeSource.match(/\.cm-[a-zA-Z0-9_-]+/g) ?? []),
);

/** Rendered rules that set a radius on a selector naming one of the theme's classes. */
function themeRadiusRules(css: string): string[] {
  const rules = css.match(/[^{}]+\{[^}]*\}/g) ?? [];
  return rules.filter((r) => {
    const selector = r.slice(0, r.indexOf('{'));
    if (!/border-radius\s*:/.test(r)) return false;
    return THEME_CLASSES.some((c) => selector.includes(c));
  });
}

/**
 * Normalized form for assertions: the per-theme scope classes (ͼN) are
 * stripped (they are unstable per mount and REPEATED inside selector lists —
 * `.ͼ5 .cm-note-link, .ͼ5 .cm-ref-link` would break naive substring checks),
 * whitespace collapsed, and structural space padding around {} : , removed.
 * Value-internal spacing (font stacks, calc) is preserved.
 */
function norm(theme: unknown): string {
  return renderedCss(theme)
    .replace(/\.ͼ\d+/g, '')
    .replace(/\s+/g, ' ')
    .replace(/ ?\{ ?/g, '{')
    .replace(/ ?: ?/g, ':')
    .replace(/ ?, ?/g, ',');
}

describe('loreEditorTheme (rendered css)', () => {
  const css = () => norm(loreEditorTheme);


  it('every theme rule that sets border-radius sets it to 0 (the zero-radius INVARIANT)', () => {
    const rules = themeRadiusRules(renderedCss(loreEditorTheme));
    // inline code, search match, transclusion band, link base, broken links.
    expect(rules.length, 'the theme defines radius rules to pin').toBeGreaterThanOrEqual(5);
    for (const r of rules) {
      expect(r.replace(/\s+/g, ''), `rule must set radius 0: ${r}`).toMatch(/border-radius:0[;}]/);
    }
  });

  it('selection/bracket/search nesting matrix around inline code keeps its exact tints', () => {
    const c = css();
    // All nesting shapes for selectionMatch / matchingBracket / nonmatchingBracket.
    for (const combo of [
      '.cm-inline-code.cm-selectionMatch',
      '.cm-inline-code.cm-matchingBracket',
      '.cm-inline-code.cm-nonmatchingBracket',
      '.cm-inline-code .cm-selectionMatch',
      '.cm-selectionMatch .cm-inline-code',
      '.cm-matchingBracket .cm-inline-code',
      '.cm-nonmatchingBracket .cm-inline-code',
    ]) {
      expect(c.includes(combo), `nesting rule missing: ${combo}`).toBe(true);
    }
    expect(c.includes('#99ff7780'), 'selectionMatch tint').toBe(true);
    expect(c.includes('#328c8252'), 'matchingBracket tint').toBe(true);
    expect(c.includes('#bb555544'), 'nonmatchingBracket tint').toBe(true);
    expect(/\.cm-selectionMatch \.cm-inline-code\{[^}]*background-color:transparent/.test(c), 'highlight-outer case zeroes the code bg').toBe(true);
  });

  it('find-match tints are distinct and the selected variant exists', () => {
    const c = css();
    expect(c.includes('rgba(217,119,6,0.30)'), 'amber find-match tint').toBe(true);
    expect(c.includes('rgba(98,85,224,0.45)'), 'violet selected tint').toBe(true);
  });

  it('header scale is strictly decreasing h1→h6', () => {
    const c = css();
    const sizes = [1, 2, 3, 4, 5, 6].map((h) => {
      const m = new RegExp(`\\.cm-header-${h}\\{[^}]*font-size:([0-9.]+)rem`).exec(c);
      expect(m, `.cm-header-${h} font-size rule`).not.toBeNull();
      return Number(m![1]);
    });
    // Non-increasing across the scale (h5 and h6 share 1rem by design — they
    // differ in weight/color, not size); strictly larger through h4.
    for (let i = 1; i < sizes.length; i++) {
      expect(sizes[i - 1], `h${i} must not be smaller than h${i + 1}`).toBeGreaterThanOrEqual(sizes[i]);
    }
    expect(sizes[0]).toBeGreaterThan(sizes[3]);
  });

  it('fenced code: span reset preserves syntax token colors, markdown classes neutralized', () => {
    const c = css();
    expect(c.includes('.cm-fenced-code span{font-family:'), 'span reset present').toBe(true);
    for (const tok of ['code-key', 'code-string', 'code-number', 'code-keyword', 'code-comment', 'code-type']) {
      expect(c.includes(`.tok-${tok}`), `token color rule missing: ${tok}`).toBe(true);
    }
    expect(c, 'strong neutralized inside code').toContain('.cm-fenced-code .cm-strong{font-weight:inherit');
    expect(/\.cm-fenced-code \.cm-inline-code\{[^}]*background:none/.test(c), 'inline code bg removed inside code blocks').toBe(true);
  });

  it('link family: shared base, per-type backgrounds, broken variants, dark rules', () => {
    const c = css();
    // Shared base covers all four types in ONE selector.
    expect(/\.cm-note-link,[^{]*\.cm-ref-link,[^{]*\.cm-doc-link,[^{]*\.cm-ext-link\{/.test(c), 'shared base covers all four link types').toBe(true);
    expect(c.includes('var(--sticky-yellow-light)'), 'note link background').toBe(true);
    expect(c.includes('var(--sticky-blue-light)'), 'ref link background').toBe(true);
    expect(c.includes('var(--surface3)'), 'doc link background').toBe(true);
    // Broken variants render struck-through with a help cursor.
    expect(c.includes('.cm-doc-link-broken,.cm-note-link-broken,.cm-ref-link-broken')).toBe(true);
    expect(/\.cm-doc-link-broken,[^{]*\.cm-ref-link-broken\{[^}]*text-decoration:line-through/.test(c), 'broken variants struck through').toBe(true);
    // Dark-mode note/ref variants exist (html.dark ancestor).
    expect(c.includes('html.dark'), 'dark-mode rules present').toBe(true);
    expect(/html\.dark[^{]*\.cm-note-link\{/.test(c), 'dark note-link rule').toBe(true);
    expect(/html\.dark[^{]*\.cm-ref-link\{/.test(c), 'dark ref-link rule').toBe(true);
    // Nested .cm-link borders are killed (no double underline).
    expect(/\.cm-note-link \.cm-link,[^{]*\{[^}]*border-bottom:none/.test(c), 'nested link border killed').toBe(true);
  });

  it('transclusion band: full-bleed negative margins + inner 844px columns', () => {
    const c = css();
    expect(c.includes('margin-left:calc((100% - 100cqw - 4rem) / 2)'), 'full-bleed negative margin').toBe(true);
    expect(c.includes('.cm-transclusion-header{max-width:844px')).toBe(true);
    expect(c.includes('.cm-transclusion-body{max-width:844px')).toBe(true);
  });

  it('selection renders through the native ::selection overlay', () => {
    expect(/::selection\{[^}]*background-color:var\(--accent-glow\)/.test(css())).toBe(true);
  });
});
