/**
 * Tests for the find-match highlighter (plan Part A).
 *
 * collectMatches is pure (no DOM): a real EditorState is built per case and the
 * same query options the Find panel exposes are exercised. The ViewPlugin test
 * pins the core fix — decorations derive from the QUERY, not the selection.
 */
// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { SearchQuery, setSearchQuery, search } from '@codemirror/search';
import { collectMatches, searchHighlight, searchHighlightPlugin, SEARCH_HIGHLIGHT_LIMIT } from './find-matches';

function state(doc: string) {
  return EditorState.create({ doc });
}

function ranges(doc: string, opts: ConstructorParameters<typeof SearchQuery>[0]) {
  return collectMatches(state(doc), new SearchQuery(opts)).ranges;
}

describe('collectMatches', () => {
  it('finds every plain substring', () => {
    const r = ranges('foo bar foo baz foo', { search: 'foo' });
    expect(r).toEqual([
      { from: 0, to: 3 },
      { from: 8, to: 11 },
      { from: 16, to: 19 },
    ]);
  });

  it('is case-insensitive by default and case-sensitive when asked', () => {
    expect(ranges('Foo foo FOO', { search: 'foo' })).toHaveLength(3);
    expect(ranges('Foo foo FOO', { search: 'foo', caseSensitive: true })).toEqual([
      { from: 4, to: 7 },
    ]);
  });

  it('honors wholeWord — word-boundary neighbors are excluded', () => {
    // "cat" appears in "cat", "cats", "concat". wholeWord keeps only the bare word.
    expect(ranges('cat cats concat cat', { search: 'cat', wholeWord: true })).toEqual([
      { from: 0, to: 3 },
      { from: 16, to: 19 },
    ]);
  });

  it('interprets the query as a regexp when regexp:true', () => {
    expect(ranges('a1 b2 c3', { search: '[a-z][0-9]', regexp: true })).toEqual([
      { from: 0, to: 2 },
      { from: 3, to: 5 },
      { from: 6, to: 8 },
    ]);
  });

  it('zero-length regexp matches do not loop forever and yield no empty ranges', () => {
    const { ranges: r } = collectMatches(
      state('aaa'),
      new SearchQuery({ search: 'a*', regexp: true }),
    );
    // No empty {from:x,to:x} ranges survive; no hang.
    expect(r.every(m => m.to > m.from)).toBe(true);
  });

  it('returns an empty set (not truncated) for an empty/invalid query', () => {
    expect(collectMatches(state('abc'), new SearchQuery({ search: '' }))).toEqual({
      ranges: [],
      truncated: false,
    });
    // invalid regexp → valid:false → empty, not a throw.
    expect(collectMatches(state('abc'), new SearchQuery({ search: '(abc', regexp: true }))).toEqual({
      ranges: [],
      truncated: false,
    });
  });

  it('caps the walk at the limit and reports truncation', () => {
    const doc = 'x '.repeat(500); // 500 "x" matches
    const res = collectMatches(state(doc), new SearchQuery({ search: 'x' }), 10);
    expect(res.ranges).toHaveLength(10);
    expect(res.truncated).toBe(true);
  });

  it('matches the set selectMatches/replaceAll act on (whole-doc, no viewport bias)', () => {
    // A long doc: every match is found regardless of a viewport a real editor
    // would have truncated. This is the point of walking matchAll, not visibleRanges.
    const doc = 'ab '.repeat(400);
    const r = collectMatches(state(doc), new SearchQuery({ search: 'ab' })).ranges;
    expect(r).toHaveLength(400);
    expect(r[0]).toEqual({ from: 0, to: 2 });
    expect(r[399]).toEqual({ from: 1197, to: 1199 });
  });
});

describe('searchHighlight decoration', () => {
  // The regression this whole feature fixes: highlightSelectionMatches keys on the
  // selected TEXT, so with an empty selection nothing is highlighted. The plugin must
  // decorate from the QUERY alone — query set, selection empty ⇒ matches still marked.
  it('decorates matches from the query, not the selection', () => {
    const view = new EditorView({
      state: EditorState.create({
        doc: 'foo bar foo',
        // selection intentionally empty (collapsed at 0).
        extensions: [search(), searchHighlight],
      }),
      parent: document.body,
    });
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: 'foo' })) });

    const set = view.plugin(searchHighlightPlugin)!.decorations;
    expect(set.size).toBe(2);
    const froms: number[] = [];
    set.between(0, view.state.doc.length, (from) => { froms.push(from); });
    expect(froms).toEqual([0, 8]);

    view.destroy();
  });

  it('marks the match under the primary selection as selected', () => {
    const view = new EditorView({
      state: EditorState.create({
        doc: 'foo foo foo',
        extensions: [search(), searchHighlight],
      }),
      parent: document.body,
    });
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: 'foo' })) });
    // Land the selection on the 2nd match — mirroring findNext.
    view.dispatch({ selection: { anchor: 4, head: 7 } });

    const set = view.plugin(searchHighlightPlugin)!.decorations;
    const classes: string[] = [];
    set.between(0, view.state.doc.length, (_from, _to, deco) => {
      classes.push(deco.spec.class);
    });
    expect(classes).toContain('cm-searchMatch cm-searchMatch-selected');
    expect(classes.filter(c => c === 'cm-searchMatch').length).toBe(2);

    view.destroy();
  });

  it('clears decorations when the query is cleared', () => {
    const view = new EditorView({
      state: EditorState.create({
        doc: 'foo foo',
        extensions: [search(), searchHighlight],
      }),
      parent: document.body,
    });
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: 'foo' })) });
    expect(view.plugin(searchHighlightPlugin)!.decorations.size).toBe(2);
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: '' })) });
    expect(view.plugin(searchHighlightPlugin)!.decorations.size).toBe(0);
    view.destroy();
  });

  it('respects SEARCH_HIGHLIGHT_LIMIT (over-cap docs still decorate, just not all)', () => {
    const doc = 'z '.repeat(SEARCH_HIGHLIGHT_LIMIT + 50);
    const view = new EditorView({
      state: EditorState.create({
        doc,
        extensions: [search(), searchHighlight],
      }),
      parent: document.body,
    });
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: 'z' })) });
    expect(view.plugin(searchHighlightPlugin)!.decorations.size).toBe(SEARCH_HIGHLIGHT_LIMIT);
    view.destroy();
  });
});
