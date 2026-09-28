/** Parse outgoing links from markdown content. Extracted from OutgoingLinksPanel. */
// SYSTEM: outgoing-links — shared link parser (used by LinksPanel)
// ARCH: Pure function extracted from deleted OutgoingLinksPanel. Shared by LinksPanel for combined
// incoming+outgoing view. No React dependencies — testable in isolation.

import { docLink, refLink, extLink } from '../components/editor/link-patterns';

export interface OutgoingLink {
  label: string;
  target: string;
  type: 'doc' | 'ref' | 'ext';
}

export function parseOutgoingLinks(content: string): OutgoingLink[] {
  const links: OutgoingLink[] = [];
  const seen = new Set<string>();

  for (const [regex, type] of [
    [docLink(), 'doc'],
    [refLink(), 'ref'],
    [extLink(), 'ext'],
  ] as const) {
    let m: RegExpExecArray | null;
    while ((m = regex.exec(content))) {
      const key = `${type}:${m[2]}`;
      if (!seen.has(key)) {
        seen.add(key);
        links.push({ label: m[1], target: m[2], type });
      }
    }
  }
  return links;
}
