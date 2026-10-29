/** filterSessionsByTitle — title-only, case-insensitive substring filter; the length gate is the caller's. */
import { describe, it, expect } from 'vitest';
import { filterSessionsByTitle } from './useSortedSessions';
import type { ChatSession } from '../../types';

const s = (id: string, title: string) => ({ session_id: id, title } as ChatSession);

describe('filterSessionsByTitle', () => {
  it('Cyrillic: decomposed uppercase Ё in the query matches a precomposed ё title', () => {
    const ru = [s('r1', 'ёжик в тумане'), s('r2', 'Ёжики'), s('r3', 'ежевика')];
    // "Ё" typed as Е + combining diaeresis (U+0308).
    expect(filterSessionsByTitle(ru, 'Е\u0308жи').map(x => x.session_id)).toEqual(['r1', 'r2']);
    expect(filterSessionsByTitle(ru, 'ЁЖИ').map(x => x.session_id)).toEqual(['r1', 'r2']);
    expect(filterSessionsByTitle([s('t', 'Рассказ про ёжика в тумане')], 'Ёжи').map(x => x.session_id)).toEqual(['t']);
  });
  it('Latin Ë (U+00CB, Mac Option+U) is read as Cyrillic ё', () => {
    const ru = [s('t', 'Рассказ про ёжика в тумане')];
    expect(filterSessionsByTitle(ru, '\u00cbжи').map(x => x.session_id)).toEqual(['t']);
    expect(filterSessionsByTitle(ru, '\u00ebжи').map(x => x.session_id)).toEqual(['t']);
  });
  it('ё and е are interchangeable', () => {
    const ru = [s('r1', 'ёжик'), s('r2', 'ежик')];
    expect(filterSessionsByTitle(ru, 'ежи').map(x => x.session_id)).toEqual(['r1', 'r2']);
    expect(filterSessionsByTitle(ru, 'ёжи').map(x => x.session_id)).toEqual(['r1', 'r2']);
  });
});
