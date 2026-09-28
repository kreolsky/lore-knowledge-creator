/** Unit tests for the shared transclusion-grammar module (parseTarget + resolver + Lezer helper). */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { syntaxTree } from '@codemirror/language';
import {
  parseTarget,
  resolveTransclusionTarget,
  extractEmbedFromImageNode,
} from './transclusion-grammar';
import { transcludeMap } from './effects';

// ── parseTarget (pure, no map) ───────────────────────────────────────────────

describe('parseTarget', () => {
  it('parses a ref: scheme', () => {
    expect(parseTarget('ref:abc')).toEqual({ scheme: 'ref', id: 'abc' });
  });
  it('parses a doc: scheme', () => {
    expect(parseTarget('doc:xyz')).toEqual({ scheme: 'doc', id: 'xyz' });
  });
  it('parses a bare id (document transclusion)', () => {
    expect(parseTarget('some-doc-id_1')).toEqual({ scheme: 'bare-doc', id: 'some-doc-id_1' });
  });
  // table: is a doc-local transclusion target (id is a key in the host doc's `tables` map).
  it('parses a table: scheme', () => {
    expect(parseTarget('table:t-uuid-1')).toEqual({ scheme: 'table', id: 't-uuid-1' });
  });
  // INVARIANT: the reject set MUST be identical on both FE and BE. `data:` was missing
  // from the frontend's old reject regex (build-structural) — the shared grammar closes it.  Why: the reject set must match FE and BE; `data:` was missing from the frontend's old regex, so the shared grammar closes the gap.
  it('returns null for a data: URI', () => {
    expect(parseTarget('data:image/png;base64,iVBORw0KGgo=')).toBeNull();
  });
  it('returns null for http(s) URLs', () => {
    expect(parseTarget('https://example.com/x.png')).toBeNull();
    expect(parseTarget('http://example.com/x.png')).toBeNull();
  });
  it('returns null for mailto:', () => {
    expect(parseTarget('mailto:a@b.com')).toBeNull();
  });
  it('returns null for # fragments', () => {
    expect(parseTarget('#anchor')).toBeNull();
  });
  // note: is a note-link handled elsewhere (resolveLinkCls → cm-note-link); it is NOT a
  // transclusion and must resolve to null.
  it('returns null for note: scheme', () => {
    expect(parseTarget('note:thread1')).toBeNull();
  });
});

// ── resolveTransclusionTarget (parseTarget + map lookup) ─────────────────────

describe('resolveTransclusionTarget', () => {
  beforeEach(() => transcludeMap.clear());

  it('resolves a ref-image entry as image', () => {
    transcludeMap.set('i1', { kind: 'ref-image', title: 'I', imageUrl: 'u' });
    expect(resolveTransclusionTarget('ref:i1')).toEqual({ kind: 'image', id: 'i1' });
  });
  it('resolves a ref-file entry as file (agent archive → download card)', () => {
    transcludeMap.set('z1', { kind: 'ref-file', title: 'Z', fileUrl: '/api/files/z1/page.zip', fileSize: 4096 });
    expect(resolveTransclusionTarget('ref:z1')).toEqual({ kind: 'file', id: 'z1' });
  });
  it('resolves a ref-text entry as text', () => {
    transcludeMap.set('t1', { kind: 'ref-text', title: 'T', content: 'c' });
    expect(resolveTransclusionTarget('ref:t1')).toEqual({ kind: 'text', id: 't1' });
  });
  it('resolves a doc: entry as doc', () => {
    transcludeMap.set('d1', { kind: 'doc', title: 'D', content: 'c' });
    expect(resolveTransclusionTarget('doc:d1')).toEqual({ kind: 'doc', id: 'd1' });
  });
  it('resolves a bare id doc entry as doc', () => {
    transcludeMap.set('d2', { kind: 'doc', title: 'D', content: 'c' });
    expect(resolveTransclusionTarget('d2')).toEqual({ kind: 'doc', id: 'd2' });
  });
  it('returns null for an unresolved ref id', () => {
    expect(resolveTransclusionTarget('ref:missing')).toBeNull();
  });
  it('returns null for a non-transclusion target', () => {
    expect(resolveTransclusionTarget('data:image/gif;base64,R0lGODlh=')).toBeNull();
    expect(resolveTransclusionTarget('https://x.com')).toBeNull();
    expect(resolveTransclusionTarget('note:t')).toBeNull();
  });
  // table: is parsed as a transclusion but resolved doc-locally by the widget dispatch,
  // never via transcludeMap — so the generic resolver returns null.
  it('returns null for a table: target (doc-local, not in transcludeMap)', () => {
    transcludeMap.set('t-uuid-1', { kind: 'doc', title: 'X', content: 'c' });
    expect(resolveTransclusionTarget('table:t-uuid-1')).toBeNull();
  });
});

// ── extractEmbedFromImageNode (Lezer helper, Task 3) ─────────────────────────

function buildState(doc: string): { state: EditorState; view: EditorView } {
  const state = EditorState.create({
    doc,
    extensions: [markdown({ base: markdownLanguage })],
  });
  const view = new EditorView({ state, parent: document.createElement('div') });
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  return { state, view };
}

describe('extractEmbedFromImageNode', () => {
  it('extracts urlText + alt + absolute range from a standalone image node', () => {
    const { state } = buildState('![alt](ref:abc)');
    let found: ReturnType<typeof extractEmbedFromImageNode> = null;
    let imgFrom = 0;
    let imgTo = 0;
    syntaxTree(state).iterate({
      enter(node) {
        if (node.name === 'Image') {
          imgFrom = node.from;
          imgTo = node.to;
          found = extractEmbedFromImageNode(node.node, state);
        }
      },
    });
    expect(found).not.toBeNull();
    expect(found!.urlText).toBe('ref:abc');
    expect(found!.alt).toBe('alt');
    // range MUST be absolute doc coordinates (matches the inline decoration range).
    expect(found!.range).toEqual({ from: imgFrom, to: imgTo });
  });

  it('returns null for a non-image node', () => {
    const { state } = buildState('**bold**');
    let result: ReturnType<typeof extractEmbedFromImageNode> = null;
    syntaxTree(state).iterate({
      enter(node) {
        if (node.name === 'Emphasis' && result === null) {
          result = extractEmbedFromImageNode(node.node, state);
        }
      },
    });
    expect(result).toBeNull();
  });

  it('extracts from an inline image node (mid-paragraph)', () => {
    const { state } = buildState('see ![pic](doc:d1) end');
    let found: ReturnType<typeof extractEmbedFromImageNode> = null;
    syntaxTree(state).iterate({
      enter(node) {
        if (node.name === 'Image' && !found) {
          found = extractEmbedFromImageNode(node.node, state);
        }
      },
    });
    expect(found!.urlText).toBe('doc:d1');
    expect(found!.alt).toBe('pic');
  });

  it('accepts prebuilt children and skips its own walk (parity with caller-supplied)', () => {
    // build-structural walks children once for bracket-hiding, then passes them here so the
    // helper doesn't re-walk. The result must match the self-walked path byte-for-byte.
    const { state } = buildState('![alt](ref:abc)');
    let selfWalked: ReturnType<typeof extractEmbedFromImageNode> = null;
    let prebuilt: ReturnType<typeof extractEmbedFromImageNode> = null;
    let imageNode: import('@lezer/common').SyntaxNode | null = null;
    syntaxTree(state).iterate({
      enter(node) {
        if (node.name === 'Image' && !imageNode) imageNode = node.node;
      },
    });
    // Self-walked (no prebuiltChildren).
    selfWalked = extractEmbedFromImageNode(imageNode!, state);
    // Caller walks the same children manually.
    const children: { name: string; from: number; to: number }[] = [];
    const cursor = imageNode!.cursor();
    if (cursor.firstChild()) {
      do {
        children.push({ name: cursor.name, from: cursor.from, to: cursor.to });
      } while (cursor.nextSibling());
    }
    prebuilt = extractEmbedFromImageNode(imageNode!, state, children);
    expect(prebuilt).toEqual(selfWalked);
  });
});
