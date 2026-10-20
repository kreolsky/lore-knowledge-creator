/**
 * Note anchor resolution states (ok / pending / orphan) over live Y.Docs. Every case
 * carries Cyrillic AND 😀 on both sides of the anchor — UTF-16 vs code-point mixing
 * is the failure class this guards.
 */
import { describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { buildNoteAnchor, resolveNoteAnchor, type NoteAnchorState } from '../note-anchor-wire';

const BEFORE = 'Привет 😀 ';
const FRAGMENT = 'мир😀край';
const AFTER = ' конец 😀';

function docWith(clientID: number, text?: string): Y.Doc {
  const doc = new Y.Doc();
  doc.clientID = clientID;
  if (text !== undefined) doc.getText('content').insert(0, text);
  return doc;
}

function sync(from: Y.Doc, to: Y.Doc): void {
  Y.applyUpdate(to, Y.encodeStateAsUpdate(from, Y.encodeStateVector(to)));
}

/** A doc holding BEFORE+FRAGMENT+AFTER and the wire anchor over FRAGMENT. */
function anchored(): { doc: Y.Doc; relStart: string; relEnd: string } {
  const doc = docWith(1, BEFORE + FRAGMENT + AFTER);
  const from = BEFORE.length;
  const anchor = buildNoteAnchor(doc.getText('content'), from, from + FRAGMENT.length);
  return { doc, relStart: anchor.relStart, relEnd: anchor.relEnd };
}

function quoteOf(doc: Y.Doc, res: NoteAnchorState | null): string | null {
  return res?.state === 'ok' ? doc.getText('content').toString().slice(res.from, res.to) : null;
}

describe('resolveNoteAnchor', () => {
  it('resolves to the anchored fragment', () => {
    const { doc, relStart, relEnd } = anchored();
    expect(quoteOf(doc, resolveNoteAnchor(relStart, relEnd, doc))).toBe(FRAGMENT);
  });

  it('typing exactly at either boundary does not grow the note', () => {
    const { doc, relStart, relEnd } = anchored();
    const text = doc.getText('content');
    text.insert(BEFORE.length + FRAGMENT.length, 'ЁЖ😀');
    text.insert(BEFORE.length, '😀ЯЮ');
    expect(quoteOf(doc, resolveNoteAnchor(relStart, relEnd, doc))).toBe(FRAGMENT);
  });

  it('follows the fragment when text moves around it', () => {
    const { doc, relStart, relEnd } = anchored();
    doc.getText('content').insert(0, 'Новое 😀 начало\n');
    expect(quoteOf(doc, resolveNoteAnchor(relStart, relEnd, doc))).toBe(FRAGMENT);
  });

  it('deleting the fragment makes the note an orphan', () => {
    const { doc, relStart, relEnd } = anchored();
    doc.getText('content').delete(BEFORE.length, FRAGMENT.length);
    expect(resolveNoteAnchor(relStart, relEnd, doc)).toEqual({ state: 'orphan' });
  });

  it('an undo of the deletion on a second Y.Doc does not revive the note on either doc', () => {
    const { doc: a, relStart, relEnd } = anchored();
    const b = docWith(2);
    sync(a, b);
    const textB = b.getText('content');
    const undo = new Y.UndoManager(textB, { captureTimeout: 0 });
    textB.delete(BEFORE.length, FRAGMENT.length);
    undo.undo();
    expect(textB.toString()).toBe(BEFORE + FRAGMENT + AFTER);
    sync(b, a);
    expect(resolveNoteAnchor(relStart, relEnd, b)).toEqual({ state: 'orphan' });
    expect(resolveNoteAnchor(relStart, relEnd, a)).toEqual({ state: 'orphan' });
  });

  it('a note that arrives before its Yjs update is pending, then resolves', () => {
    const base = docWith(1, BEFORE + AFTER);
    const peer = docWith(2);
    sync(base, peer);
    base.getText('content').insert(BEFORE.length, FRAGMENT);
    const anchor = buildNoteAnchor(base.getText('content'), BEFORE.length, BEFORE.length + FRAGMENT.length);
    expect(resolveNoteAnchor(anchor.relStart, anchor.relEnd, peer)).toEqual({ state: 'pending' });
    sync(base, peer);
    expect(quoteOf(peer, resolveNoteAnchor(anchor.relStart, anchor.relEnd, peer))).toBe(FRAGMENT);
  });

  it('a legacy (non-base64) anchor resolves to null', () => {
    const doc = docWith(1, BEFORE);
    expect(resolveNoteAnchor('{"type":null}', '{"type":null}', doc)).toBeNull();
  });
});
