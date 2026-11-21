/** The `ref:` id grammar over a markdown text — shared by the link resolver and cross-project paste. */

import { LINK_TYPES } from '../components/editor/live-preview/link-types';
import { parseTarget } from '../components/editor/live-preview/transclusion-grammar';

// see SYSTEM: link-types — the ref entry's match is the single source of the
// `[x](ref:id)` grammar (its `!?` also covers labeled embeds `![x](ref:id)`).
const REF_LINK_ENTRY = LINK_TYPES.find((e) => e.kind === 'ref')!;

/** Collect every ref: id present in a text, from BOTH link forms:
 * `[x](ref:id)` links (via the LINK_TYPES ref entry — the same grammar the
 * decorations use) and `![…](ref:id)` embeds including the EMPTY-label form the
 * ref entry's `[^\]]+` label cannot match (via parseTarget, the same grammar the
 * lazy-fetch effect uses). Duplicate ids collapse — the resolver probes SETS. */
export function collectRefIdsFromText(text: string): Set<string> {
  const ids = new Set<string>();
  for (const m of REF_LINK_ENTRY.match(text)) ids.add(m.id);
  const embedRe = /!\[[^\]]*\]\(([^)]+)\)/g;
  let m: RegExpExecArray | null;
  while ((m = embedRe.exec(text)) !== null) {
    const parsed = parseTarget(m[1]);
    if (parsed?.scheme === 'ref') ids.add(parsed.id);
  }
  return ids;
}
