/** Public share API — anonymous read client + owner-gated share CRUD.
 *
 * SYSTEM: public-share (FE) — the anonymous read-only surface for a published
 * doc/subtree, plus the owner-facing mint/list/revoke wrappers used by the
 * Access tab.
 *
 * ARCH (plan "iridescent-wibbling-heron"): the anonymous endpoints carry the
 * share token in the URL path (NOT a header) — the public page URL IS the link
 * (/s/:token). The token never round-trips through the API client's auth path:
 * the public router returns 404 (not 401) for invalid/expired tokens, so
 * apiClient's redirect-on-401 guard never fires here.
 *
 * Notes are on the public surface only while the covering share has
 * `allow_comments` — the public* note calls below (backend routes/public_notes.py).
 * The public Editor reads `tables_json` from `publicDocumentByDoc` to seed its table
 * widgets (mirrors snapshot-preview seeding).
 */
// ARCH: apiClient is safe for public endpoints — they return 404 on bad tokens,
// never 401, so the redirect-on-401 guard in client.ts cannot misfire.

import { apiClient } from './client';
import type { HeadingItem, Reference } from '../types';

/** A node in the public tree payload (subtree-scope; doc-scope = root only). */
export interface PublicTreeNode {
  id: string;
  title: string;
  parent_id: string | null;
  sort_key?: string | null;
  /** ISO-8601 — nothing reads them yet in the FE; the type documents the payload
   *  (external consumers, e.g. the blog PoC, order and feed off these). */
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PublicTreeResponse {
  nodes: PublicTreeNode[];
  scope: 'doc' | 'subtree';
  root_id: string;
  /** Owning project's name — shown as the public header breadcrumb root. */
  project_name: string;
}

export interface PublicDocumentResponse {
  document_id: string;
  title: string;
  content: string;
  /** Live Yjs tables subtree (JSON) — seeds the public Editor's table widgets.
   *  Null when no live state exists (legacy/no-tables doc); the editor then renders
   *  the markdown anchors as text. */
  tables_json: string | null;
  headings: HeadingItem[];
  /** Fingerprint of `content`, echoed back when a guest anchors a note to it. */
  content_sha256: string;
}

/** Anonymous tree read — no auth, token in path. The ONLY token-keyed survivor:
 *  used by TokenRedirect to resolve a legacy /s/:token link to its share-root
 *  document_id before redirecting to /docs/<id>. Every other public read is
 *  document-keyed (below). */
export async function publicTree(token: string): Promise<PublicTreeResponse> {
  return apiClient.get(`/public/${token}/tree`);
}

/**
 * Document-keyed anonymous reads (plan "public-document-ids").
 *
 * ARCH: the canonical public surface is keyed on the document's own uuid, NOT the
 * share token — the token is a legacy alias only. resolve_share (backend) walks
 * ancestors to find the live share, so any document_id in a published subtree
 * resolves. These power the /docs/:id anonymous branch; the token-keyed publicTree
 * above survives only for the /s/:token → /docs/:id redirect resolver.
 */
export async function publicTreeByDoc(documentId: string): Promise<PublicTreeResponse> {
  return apiClient.get(`/public/documents/${documentId}/tree`);
}
export async function publicDocumentByDoc(documentId: string): Promise<PublicDocumentResponse> {
  return apiClient.get(`/public/documents/${documentId}`);
}
export async function publicReferencesByDoc(documentId: string): Promise<Reference[]> {
  const data = await apiClient.get(`/public/documents/${documentId}/references`);
  return data.references;
}

/**
 * Public notes (comments on a public link). Offsets are UTF-16 over the payload's
 * `content` — the public editor's own positions. No member or guest id ever reaches
 * this surface: authorship is `author_name` + the viewer's own `is_mine`.
 */
export interface PublicNoteAnchor {
  state: 'ok' | 'pending' | 'orphan';
  start: number | null;
  end: number | null;
}

export interface PublicNoteMessage {
  message_id: string;
  parent_id: string | null;
  content: string;
  created_at: string | null;
  author_name: string | null;
  is_mine: boolean;
}

export interface PublicNote {
  session_id: string;
  created_at: string | null;
  anchor: PublicNoteAnchor | null;
  anchor_quote: string | null;
  is_mine: boolean;
  messages: PublicNoteMessage[];
}

export interface PublicNotesResponse {
  allow_comments: boolean;
  /** The text the anchors were resolved against (null: no anchored note). */
  content_sha256: string | null;
  notes: PublicNote[];
}

/** A guest's selection: UTF-16 offsets + the served payload's fingerprint. */
export interface PublicNoteSelection {
  start: number;
  end: number;
  content_sha256: string;
}

export async function publicNotesByDoc(documentId: string): Promise<PublicNotesResponse> {
  return apiClient.get(`/public/documents/${documentId}/notes`);
}
export async function createPublicNote(
  documentId: string, body: { author_name: string; content: string } & Partial<PublicNoteSelection>,
): Promise<PublicNote> {
  return apiClient.post(`/public/documents/${documentId}/notes`, body);
}
export async function replyPublicNote(
  documentId: string, sessionId: string,
  body: { author_name: string; content: string; parent_id: string | null },
): Promise<PublicNoteMessage> {
  return apiClient.post(`/public/documents/${documentId}/notes/${sessionId}/messages`, body);
}
export async function deletePublicNoteMessage(
  documentId: string, sessionId: string, messageId: string,
): Promise<void> {
  await apiClient.delete(`/public/documents/${documentId}/notes/${sessionId}/messages/${messageId}`);
}
export async function deletePublicNote(documentId: string, sessionId: string): Promise<void> {
  await apiClient.delete(`/public/documents/${documentId}/notes/${sessionId}`);
}

/** Owner-facing share management (used by the Access tab). */
export interface DocumentShare {
  share_id: string;
  scope: 'doc' | 'subtree';
  token: string;
  /** Link holders may leave notes (the text stays read-only). */
  allow_comments?: boolean;
  created_at: string;
  created_at_fmt?: string;
  created_by?: string | null;
}

/** The nearest ancestor whose subtree share publishes a doc that has no own row.
 *  Null when the doc has its own row or no covering share anywhere. */
export interface InheritedFrom {
  document_id: string;
  title: string;
}

export interface SharesListResponse {
  shares: DocumentShare[];
  inherited_from: InheritedFrom | null;
}

export interface CreatedShare {
  share_id: string;
  token: string;
  scope: 'doc' | 'subtree';
}

/** Mint a share; plaintext token returned ONCE. */
export async function createShare(
  projectId: string, documentId: string, scope: 'doc' | 'subtree',
): Promise<CreatedShare> {
  return apiClient.post(`/projects/${projectId}/documents/${documentId}/shares`, { scope });
}
export async function listShares(projectId: string, documentId: string): Promise<SharesListResponse> {
  return apiClient.get(`/projects/${projectId}/documents/${documentId}/shares`);
}
export async function revokeShare(shareId: string): Promise<void> {
  await apiClient.delete(`/shares/${shareId}`);
}

/** Atomically change a share's scope (doc ↔ subtree). Owner-only server-side. */
export async function updateShareScope(shareId: string, scope: 'doc' | 'subtree'): Promise<void> {
  await apiClient.patch(`/shares/${shareId}`, { scope });
}

/** Turn comments on a public link on or off. Owner-only server-side. */
export async function updateShareComments(shareId: string, allowComments: boolean): Promise<void> {
  await apiClient.patch(`/shares/${shareId}`, { allow_comments: allowComments });
}
