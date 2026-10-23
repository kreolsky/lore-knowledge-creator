import { describe, it, expect } from 'vitest';
import { en } from './en';
import { ru } from './ru';

// Compile time already forces ru ⊇ en: `TranslationKey` is derived from en.ts and
// index.ts types the dictionaries as Record<Language, Record<TranslationKey, string>>.
// The direction no type checks is ru ⊆ en — a key left behind in ru.ts after its
// en.ts arm was renamed or deleted goes unnoticed forever. This test pins both.
// The count is deliberately not asserted: it must drift as keys are added.

describe('i18n key parity — en.ts and ru.ts carry the same key set', () => {
  it('no ru-only keys (dead arms left behind by en renames/deletes)', () => {
    const ruOnly = Object.keys(ru).filter(k => !(k in en));
    expect(ruOnly).toEqual([]);
  });

  it('no en-only keys (a locale missing its translation)', () => {
    const enOnly = Object.keys(en).filter(k => !(k in ru));
    expect(enOnly).toEqual([]);
  });
});
