/**
 * Shared contract for in-app entity links (ref:/doc:/note:) used by the editor,
 * chat markdown, sources list, and the hover-preview host.
 *
 * All three parties — producers (MarkdownContent, Sources, build-structural) and the
 * single consumer (EditorLinkPreview) — go through encode/decode here so the data-link-id
 * contract is code-enforced, not convention-enforced.
 *
 * Link types and their URL schemes:
 * - document → doc:<id>  (the editor also accepts a bare id as a doc link)
 * - reference → ref:<id>
 * - note → note:<id>
 *
 * // ARCH: data-link-id encoding — docs use a bare id (no prefix); refs/notes keep their
 * // scheme prefix. decodeLinkId strips ANY scheme prefix defensively for ALL types, so
 * // both bare and prefixed values resolve correctly regardless of which producer emitted
 * // them. Why: the editor (build-structural.ts) emits data-link-id = urlText verbatim —
 * // doc links are authored as bare ids, so they're already bare; ref/note urlText
 * // includes the scheme. The chat sources list (chat/Sources.tsx) calls encodeLinkId
 * // explicitly. decodeLinkId normalizes both representations at the single consumption
 * // point, eliminating the bare-vs-prefixed mismatch risk.
 */

export type InAppLinkType = 'ref' | 'doc' | 'note';

const SCHEME_RE = /^(ref|doc|note):(.+)$/;

/**
 * The chat markdown wrapper's rewritten form (plan chat-markdown-on-dsh):
 * dsh's renderer allowlists only http(s)/mailto destinations, so `doc:`/`ref:`/
 * `note:` links in PROSE segments become this absolute URL — an allowlisted
 * destination dsh renders as a real anchor, decoded back here by the chat
 * click handler and EditorLinkPreview's href-derived branch.
 */
const LORE_LOCAL_RE = /^https:\/\/lore\.local\/l\/(ref|doc|note)\/([^/?#\s]+)$/;

/** Parse an in-app link URL into type + raw id, or null if not an in-app link.
 * Accepts both the scheme form (`doc:<id>`) and the rewritten
 * `https://lore.local/l/<type>/<id>` form. */
export function parseInAppLink(url: string): { type: InAppLinkType; id: string } | null {
  const trimmed = url.trim();
  const local = trimmed.match(LORE_LOCAL_RE);
  if (local) return { type: local[1] as InAppLinkType, id: decodeURIComponent(local[2]) };
  const m = trimmed.match(SCHEME_RE);
  if (!m) return null;
  return { type: m[1] as InAppLinkType, id: m[2] };
}

/**
 * Encode a raw entity id into the data-link-id attribute value.
 * Docs: bare id (no prefix). Refs/notes: keep the scheme prefix.
 */
export function encodeLinkId(type: InAppLinkType, id: string): string {
  if (type === 'doc') return id;
  return `${type}:${id}`;
}

/**
 * Decode a data-link-id attribute value into the raw entity id.
 * Strips the scheme prefix defensively for ALL types (including doc), so both bare
 * and prefixed values resolve correctly regardless of which producer emitted them.
 */
export function decodeLinkId(type: InAppLinkType, value: string): string {
  return value.replace(new RegExp(`^${type}:`), '');
}
