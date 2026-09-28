import { describe, it, expect } from 'vitest';
import { utf16ToCp } from './utf16-cp';

describe('utf16ToCp', () => {
  it('is identity for ASCII (1 unit == 1 code point)', () => {
    const s = 'hello world';
    for (let i = 0; i <= s.length; i++) {
      expect(utf16ToCp(s, i)).toBe(i);
    }
  });

  it('counts a BMP non-ASCII char as 1 code point, 1 unit', () => {
    // é (U+00E9) is 1 UTF-16 unit and 1 code point.
    const s = 'café';
    expect(utf16ToCp(s, 4)).toBe(4);
    expect(s.length).toBe(4);
  });

  it('counts a surrogate-pair emoji as 1 code point, 2 UTF-16 units', () => {
    // 😀 is U+1F600 — 2 UTF-16 units, 1 code point. CM6 sees index 2; backend cp = 1.
    const s = 'a😀b';
    // positions: a=0, 😀=[1,3) units, b=3
    expect(utf16ToCp(s, 0)).toBe(0); // 'a'
    expect(utf16ToCp(s, 1)).toBe(1); // start of emoji
    expect(utf16ToCp(s, 3)).toBe(2); // 'b' — code point 2
    // A position INSIDE the surrogate pair (the low unit at index 2) is not a real
    // character boundary — CM6 never produces such a selection. The function advances
    // past the pair, mapping it to cp 2 (the position after the emoji).
    expect(utf16ToCp(s, 2)).toBe(2);
  });

  it('matches Python len() code-point semantics (the backend wire unit)', () => {
    // The backend `_resolve_edit_range` uses Python str.find (code points). A JS
    // selection [from, to) on "x😀y" of [1,3) (the emoji) must map to cp [1,2).
    const s = 'x😀y';
    expect(utf16ToCp(s, 1)).toBe(1);
    expect(utf16ToCp(s, 3)).toBe(2); // 'y' is code point 2
  });
});
