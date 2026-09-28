/** Tests for heading-slug utilities: slugify, dedupe, resolve, URL building. */

import { describe, it, expect } from 'vitest';
import type { HeadingItem } from '../types';
import {
  slugifyHeading,
  assignHeadingSlugs,
  resolveSlugToLine,
  buildHeadingUrl,
} from './heading-slug';

function h(text: string, line: number, level = 2): HeadingItem {
  return { level, text, line };
}

describe('slugifyHeading', () => {
  it('lowercases and hyphenates latin text', () => {
    expect(slugifyHeading('Hello World')).toBe('hello-world');
  });

  it('preserves Cyrillic (Unicode letters)', () => {
    expect(slugifyHeading('Глава первая')).toBe('глава-первая');
  });

  it('handles mixed scripts and digits', () => {
    expect(slugifyHeading('Глава 2: The Return')).toBe('глава-2-the-return');
  });

  it('strips punctuation', () => {
    expect(slugifyHeading('What?! (Really...)')).toBe('what-really');
  });

  it('strips inline markdown markers, keeping text', () => {
    expect(slugifyHeading('**Bold** and *em* and `code`')).toBe('bold-and-em-and-code');
  });

  it('keeps link text, drops link target', () => {
    expect(slugifyHeading('See [the docs](https://example.com) here')).toBe('see-the-docs-here');
  });

  it('collapses multiple separators and trims hyphens', () => {
    expect(slugifyHeading('  --a   b--  ')).toBe('a-b');
  });

  it('returns empty string for punctuation-only heading', () => {
    expect(slugifyHeading('?!... ---')).toBe('');
  });
});

describe('assignHeadingSlugs', () => {
  it('assigns unique slugs in document order with -N suffixes for duplicates', () => {
    const result = assignHeadingSlugs([h('Intro', 1), h('Intro', 5), h('Intro', 9)]);
    expect(result.map(r => r.slug)).toEqual(['intro', 'intro-2', 'intro-3']);
  });

  it('keeps distinct headings unsuffixed', () => {
    const result = assignHeadingSlugs([h('One', 1), h('Two', 3)]);
    expect(result.map(r => r.slug)).toEqual(['one', 'two']);
  });

  it('preserves original heading fields', () => {
    const result = assignHeadingSlugs([h('Intro', 7, 3)]);
    expect(result[0]).toMatchObject({ level: 3, text: 'Intro', line: 7, slug: 'intro' });
  });
});

describe('resolveSlugToLine', () => {
  const headings = [h('Intro', 1), h('Intro', 5), h('Глава', 9)];

  it('resolves a slug to its line', () => {
    expect(resolveSlugToLine(headings, 'intro-2')).toBe(5);
    expect(resolveSlugToLine(headings, 'глава')).toBe(9);
  });

  it('returns null for an unknown slug', () => {
    expect(resolveSlugToLine(headings, 'missing')).toBeNull();
  });
});

describe('buildHeadingUrl', () => {
  it('builds a canonical /docs/<id> URL without a hash', () => {
    expect(buildHeadingUrl('d1')).toBe(`${window.location.origin}/docs/d1`);
  });

  it('builds a heading URL with an encoded hash', () => {
    expect(buildHeadingUrl('d1', 'глава-первая')).toBe(
      `${window.location.origin}/docs/d1#${encodeURIComponent('глава-первая')}`,
    );
  });

  it('round-trips the slug through decodeURIComponent', () => {
    const url = buildHeadingUrl('d1', 'глава-2-the-return');
    const hash = new URL(url).hash.slice(1);
    expect(decodeURIComponent(hash)).toBe('глава-2-the-return');
  });
});
