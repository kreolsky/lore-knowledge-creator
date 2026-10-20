/**
 * Note anchors across runtimes: pycrdt-built anchors resolve here, and this
 * suite's Yjs-built fixture is what the backend resolves
 * (tests/backend/test_note_anchors.py). Regenerate that fixture with:
 *   WRITE_NOTE_ANCHOR_FIXTURE=1 npx vitest run src/collab/__tests__/note-anchor-wire.test.ts
 * and copy the JSON printed between the markers into tests/backend/fixtures/note_anchors_yjs.json.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { buildNoteAnchor, relPosFromWire, relPosToWire } from '../note-anchor-wire';

interface AnchorCase { start: string; end: string; quote: string | null }
interface AnchorFixture { update: string; text: string; anchors: AnchorCase[] }

function fromBase64(value: string): Uint8Array {
  return Uint8Array.from(atob(value), ch => ch.charCodeAt(0));
}

function toBase64(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** Resolve without following undone deletions; null quote = orphan. */
function resolveQuote(doc: Y.Doc, anchor: { start: string; end: string }): string | null {
  const start = relPosFromWire(anchor.start);
  const end = relPosFromWire(anchor.end);
  if (!start || !end) throw new Error('anchor did not decode');
  const lo = Y.createAbsolutePositionFromRelativePosition(start, doc, false);
  const hi = Y.createAbsolutePositionFromRelativePosition(end, doc, false);
  if (!lo || !hi || hi.index <= lo.index) return null;
  return doc.getText('content').toString().slice(lo.index, hi.index);
}

function docWithClient(clientID: number): Y.Doc {
  const doc = new Y.Doc();
  doc.clientID = clientID;
  return doc;
}

/** Built here, then edited around and through by a second client. */
function yjsFixture(): AnchorFixture {
  const a = docWithClient(11);
  const ta = a.getText('content');
  ta.insert(0, 'абв😀где');
  const b = docWithClient(12);
  Y.applyUpdate(b, Y.encodeStateAsUpdate(a));
  b.getText('content').insert(2, 'Ж😀x');
  Y.applyUpdate(a, Y.encodeStateAsUpdate(b));
  ta.insert(9, 'ЯЯ');
  const s = ta.toString();
  expect(s).toBe('абЖ😀xв😀ЯЯгде');
  // UTF-16 ranges: 'абЖ' [0,3) · '😀' [3,5) · 'ЯЯ' [9,11) · 'Ж😀xв' [2,7) · 'ЯЯгде' [9,14)
  const anchors = [[0, 3], [3, 5], [9, 11], [2, 7], [9, 14]].map(([from, to]) => buildNoteAnchor(ta, from, to));
  Y.applyUpdate(b, Y.encodeStateAsUpdate(a));
  const tb = b.getText('content');
  tb.insert(9, 'Ф');   // at the start edge of 'ЯЯ'
  tb.insert(12, '😀'); // at the end edge of 'ЯЯ'
  Y.applyUpdate(a, Y.encodeStateAsUpdate(b));
  ta.delete(3, 2);     // '😀' [3,5) is gone
  return {
    update: toBase64(Y.encodeStateAsUpdate(a)),
    text: ta.toString(),
    anchors: anchors.map(anchor => {
      const wire = { start: anchor.relStart, end: anchor.relEnd };
      return { ...wire, quote: resolveQuote(a, wire) };
    }),
  };
}

describe('note anchor wire', () => {
  it('round-trips a relative position through base64', () => {
    const doc = docWithClient(5);
    doc.getText('content').insert(0, 'a😀б');
    const rpos = Y.createRelativePositionFromTypeIndex(doc.getText('content'), 1, -1);
    const decoded = relPosFromWire(relPosToWire(rpos));
    expect(decoded?.item).toEqual(rpos.item);
    expect(decoded?.assoc).toBe(-1);
  });

  it('reads a legacy JSON anchor as no anchor', () => {
    expect(relPosFromWire('{"type":null,"tname":"content"}')).toBeNull();
  });

  it('binds start AFTER and end BEFORE', () => {
    const doc = docWithClient(5);
    const text = doc.getText('content');
    text.insert(0, 'xфразаy');
    const anchor = buildNoteAnchor(text, 1, 6);
    expect(relPosFromWire(anchor.relStart)?.assoc).toBe(0);
    expect(relPosFromWire(anchor.relEnd)?.assoc).toBe(-1);
    text.insert(6, 'Р');
    text.insert(1, 'Л');
    expect(resolveQuote(doc, { start: anchor.relStart, end: anchor.relEnd })).toBe('фраза');
  });

  it('resolves pycrdt-built anchors to the same fragments', () => {
    const fixture = JSON.parse(
      readFileSync(resolve(process.cwd(), 'src/collab/__fixtures__/note-anchors-pycrdt.json'), 'utf8'),
    ) as AnchorFixture;
    const doc = new Y.Doc();
    Y.applyUpdate(doc, fromBase64(fixture.update));
    expect(doc.getText('content').toString()).toBe(fixture.text);
    for (const anchor of fixture.anchors) {
      expect(resolveQuote(doc, anchor)).toBe(anchor.quote);
    }
  });

  it('builds the Yjs fixture the backend resolves', () => {
    const fixture = yjsFixture();
    expect(fixture.anchors.map(a => a.quote)).toEqual(['абЖ', null, 'ЯЯ', 'Жxв', 'ЯЯ😀где']);
    if (process.env.WRITE_NOTE_ANCHOR_FIXTURE) {
      console.log(`<<<FIXTURE\n${JSON.stringify(fixture, null, 2)}\nFIXTURE>>>`);
    }
  });
});
