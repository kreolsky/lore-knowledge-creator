/**
 * Shared contract for in-app entity links (ref:/doc:) used by the editor,
 * chat markdown, sources list, and the hover-preview host.
 *
 * Two producers set data-link-id — build-structural (editor) and Sources (chat sources
 * list) — and EditorLinkPreview consumes it, all through encode/decode here so the
 * data-link-id contract is code-enforced, not convention-enforced. Links inside a chat
 * answer carry no data-link-* attributes: MarkdownContent rewrites them to the lore.local
 * href, which parseInAppLink decodes for both its click handler and EditorLinkPreview.
 *
 * Link types and their URL schemes:
 * - document → doc:<id>  (the editor also accepts a bare id as a doc link)
 * - reference → ref:<id>
 *
 * // ARCH: data-link-id encoding — docs use a bare id (no prefix); refs keep their
 * // scheme prefix. decodeLinkId strips ANY scheme prefix defensively for ALL types, so
 * // both bare and prefixed values resolve correctly regardless of which producer emitted
 * // them. Why: the editor (build-structural.ts) emits data-link-id = urlText verbatim —
 * // doc links are authored as bare ids, so they're already bare; ref urlText
 * // includes the scheme. The chat sources list (chat/Sources.tsx) calls encodeLinkId
 * // explicitly. decodeLinkId normalizes both representations at the single consumption
 * // point, eliminating the bare-vs-prefixed mismatch risk.
 */

export type InAppLinkType = 'ref' | 'doc';

const SCHEME_RE = /^(ref|doc):([^#]+)(?:#(.*))?$/;

/**
 * The chat markdown wrapper's rewritten form:
 * dsh's renderer allowlists only http(s)/mailto destinations, so `doc:`/`ref:`
 * links in PROSE segments become this absolute URL — an allowlisted
 * destination dsh renders as a real anchor, decoded back here by the chat
 * click handler and EditorLinkPreview's href-derived branch.
 */
const LORE_LOCAL_RE = /^https:\/\/lore\.local\/l\/(ref|doc)\/([^/?#\s]+)(?:#(\S*))?$/;

export interface InAppLink {
  type: InAppLinkType;
  id: string;
  /** Raw, still percent-encoded fragment after `#` (see utils/link-fragment.ts). */
  fragment?: string;
}

/** Parse an in-app link URL into type + raw id (+ fragment), or null if not an in-app link.
 * Accepts both the scheme form (`doc:<id>#frag`) and the rewritten
 * `https://lore.local/l/<type>/<id>#frag` form. */
export function parseInAppLink(url: string): InAppLink | null {
  const trimmed = url.trim();
  const local = trimmed.match(LORE_LOCAL_RE);
  const m = local ?? trimmed.match(SCHEME_RE);
  if (!m) return null;
  const id = local ? decodeURIComponent(m[2]) : m[2];
  return m[3] ? { type: m[1] as InAppLinkType, id, fragment: m[3] } : { type: m[1] as InAppLinkType, id };
}

/**
 * Encode a raw entity id into the data-link-id attribute value.
 * Docs: bare id (no prefix). Refs: keep the scheme prefix.
 */
export function encodeLinkId(type: InAppLinkType, id: string): string {
  if (type === 'doc') return id;
  return `${type}:${id}`;
}

/**
 * Decode a data-link-id attribute value into the raw entity id.
 * Strips the scheme prefix defensively for ALL types (including doc), so both bare
 * and prefixed values resolve correctly regardless of which producer emitted them,
 * and drops a `#fragment` — the editor emits urlText verbatim, pointer included.
 */
export function decodeLinkId(type: InAppLinkType, value: string): string {
  return value.replace(new RegExp(`^${type}:`), '').split('#')[0];
}
