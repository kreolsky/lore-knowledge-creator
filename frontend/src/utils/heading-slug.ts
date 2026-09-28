/** Heading anchor slugs for shareable document links.
 *
 * GitHub-style slugs derived on demand from heading text (Unicode preserved, so
 * Cyrillic headings stay readable in the address bar). Slugs are NOT persisted —
 * they are recomputed from extractHeadings() output wherever needed, so renaming
 * a heading simply changes its slug. Duplicate headings get -2/-3 suffixes in
 * document order.
 */
// SYSTEM: heading-slug — anchor slugs + shareable URL builder for doc headings

import type { HeadingItem } from '../types';

/** Slugify heading text: strip inline markdown, keep Unicode letters/digits,
 * spaces → hyphens, collapse and trim hyphens. */
export function slugifyHeading(text: string): string {
  const plain = text
    // Markdown links: keep the text, drop the target.
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    // Inline markers: emphasis, code.
    .replace(/[*_`~]/g, '');
  return plain
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '');
}

export type SluggedHeading = HeadingItem & { slug: string };

/** Assign unique slugs in document order; duplicates get -2, -3, … suffixes. */
export function assignHeadingSlugs(headings: HeadingItem[]): SluggedHeading[] {
  const seen = new Map<string, number>();
  return headings.map(item => {
    const base = slugifyHeading(item.text);
    const count = (seen.get(base) ?? 0) + 1;
    seen.set(base, count);
    return { ...item, slug: count === 1 ? base : `${base}-${count}` };
  });
}

/** Resolve a slug to its heading line, or null when no heading matches. */
export function resolveSlugToLine(headings: HeadingItem[], slug: string): number | null {
  const match = assignHeadingSlugs(headings).find(item => item.slug === slug);
  return match ? match.line : null;
}

/** Absolute shareable URL for a document, optionally anchored to a heading slug.
 *
 * Canonical /docs/<document_id> URL (plan "public-document-ids") — one shape for
 * every audience. The projectId is no longer part of the document URL. */
export function buildHeadingUrl(documentId: string, slug?: string): string {
  const base = `${window.location.origin}/docs/${documentId}`;
  return slug ? `${base}#${encodeURIComponent(slug)}` : base;
}
