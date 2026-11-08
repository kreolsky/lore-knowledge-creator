/** Parse EXTERNAL (http/https) links from markdown content — the one relation the graph does not hold. */
// SYSTEM: outgoing-links — client parser for external links (used by LinksPanel)
// ARCH: document/reference relations come from GET /documents/{id}/edges (see SYSTEM: edges);
// external URLs are not graph nodes, so they stay a client parse of live content.

import { extLink } from '../components/editor/link-patterns';

export interface ExternalLink {
  label: string;
  target: string;
}

export function parseExternalLinks(content: string): ExternalLink[] {
  const links: ExternalLink[] = [];
  const seen = new Set<string>();
  const regex = extLink();
  let m: RegExpExecArray | null;
  while ((m = regex.exec(content))) {
    if (!seen.has(m[2])) {
      seen.add(m[2]);
      links.push({ label: m[1], target: m[2] });
    }
  }
  return links;
}
