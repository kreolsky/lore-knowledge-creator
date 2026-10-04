"""Public share router — anonymous, read-only, single-funnel.

# SYSTEM: public-share — anonymous read-only surface for a published doc/subtree.

# ARCH (plan "public-document-ids"): the funnel is keyed on the document's own
# uuid, NOT the share token. `resolve_share(document_id)` finds the live share —
# a direct row on the doc, or the nearest ancestor with scope='subtree' — and
# derives doc_ids from the share ROOT. The token column in document_shares is
# retained as a LEGACY lookup key only: the /api/public/{token}/* routes are thin
# shims that resolve token→root document_id, then delegate to the canonical
# /api/public/documents/{document_id}/* handlers (HTTP 200 alias — the plan's
# "301/alias shims"; alias chosen so the existing token-keyed suite stays green
# without redirect-following). Knowing a document_id never grants access on its
# own: a live document_shares row is still required, and the agent-config
# subtree is unshareable by ANCESTRY.

NO `get_current_user` — this is the anonymous surface. NO write/upload/WS
endpoints live on this router; the write surface is absent by construction.

# INVARIANT(security): the ONLY funnel is `resolve_share` + `doc ∈ doc_ids`.
# Why: a one-line perimeter is auditable; spreading the check across handlers
# invites drift. Every handler calls `_require_doc` (which calls resolve_share
# AND asserts membership) — there is no other read path on this router.
#
# INVARIANT(security): every non-`user` principal is readonly. Why: "доступ для
# всех только чтение" — enforced at the write boundary (no writes exist here),
# never trusted from the UI.
#
# WHY(security, ancestry): the agent-config subtree (system root +
# descendants) is UNPUBLISHABLE, guarded by ANCESTRY not the is_system flag.
# Why: only the skeleton (system root + role folders) carries is_system=true;
# everything a user writes inside it (personas, rules, skills, memory notes) is
# is_system=false (agent_config.py: "Children are plain docs"). A flag-only
# check would publish those leaves. The system root id is deterministic
# (_deterministic_id(project_id, "system_root") → "sys-system_root-<pid>") and
# has parent_id=None, so no doc sits above it — a subtree share can never cover
# it, and the per-request ancestry guard fully closes the read path. Enforced
# here (resolve) AND at the mint chokepoint (document_shares.py). A row minted
# before this guard MUST still 404 — the resolve-path guard is the backstop.
#
# ARCH(no-rate-limit): this router has NO application-layer rate limit. A previous
# per-IP pubshare bucket (180/60s) was removed because the threat model did not
# justify it: (a) the content is by-design public — "scraping" is just reading;
# (b) the token is 256-bit entropy — brute-force is cosmically infeasible;
# (c) the one expensive op (subtree_doc_ids) is already TTL-cached per-root and
# depth-capped; (d) in single-instance deploy any request flood DoSes everything
# regardless of which bucket it hits, so the right place for volumetric limits
# is the edge (CDN/reverse-proxy), not the app. Why pin this: a reflexive
# "anonymous endpoint ⇒ must rate-limit" PR would re-introduce the 429-on-honest-
# readers UX harm for no security gain. The actual abuse-flow is owner-revocation.
#
# Perf: subtree_doc_ids is a full-project scan per request — TTL-cached per share
# root with a short TTL so a browsing session (multiple endpoint hits on the same
# published root) reuses one walk. Moves reflect after the TTL expires
# (acceptable per the plan). Depth-capped at 50 (db.get_descendant_ids): a
# subtree deeper than 50 silently loses documents. This ceiling is part of the
# share contract; documented here so a regression is visible.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException
from share_guard import is_unshareable_by_ancestry

from db import extract_id, fetch_one, get_ancestor_ids, get_db
from deps import extract_headings
from models import is_ref_row
from routes.files_serve import (
    _serve_reference_file,
    _serve_thumbnail,
)
from routes.references import dedup_refs_by_id, sort_refs_by_depth_tier
from scope import subtree_doc_ids

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── doc_ids TTL cache ─────────────────────────────────────────────────────
# ARCH: subtree_doc_ids scans the whole project per call. Cache the derived
# doc_ids per share ROOT with a short TTL so a browsing session (multiple
# endpoint hits on the same published root) reuses one walk. Moves reflect after
# the TTL expires (acceptable per the plan). Doc-scope shares are not cached —
# the set is the singleton [root]. Cache key was the token (pre-rekey); it is now
# the share-root document_id (same cardinality — one entry per published root).
_DOC_IDS_TTL_S = 10.0
# ARCH(eviction): prune-on-write bounds the cache to the active root set. A root
# accessed once would otherwise live forever — TTL governs freshness only, not
# eviction. The cap is a hard guard against pathological churn of distinct fresh
# roots; above it the lowest-expiry entry drops.
_DOC_IDS_CACHE_CAP = 2048
_doc_ids_cache: dict[str, tuple[list[str], float]] = {}


async def _resolve_doc_ids(
    project_id: str, root: str, scope: str, cache_key: str,
) -> list[str]:
    """Derive the readable doc set for a share, TTL-cached for subtree scope."""
    if scope == "doc":
        return [root]
    now = time.monotonic()
    cached = _doc_ids_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]
    ids = await subtree_doc_ids(root, project_id)
    # prune-on-write: drop entries whose TTL has expired so the cache tracks the
    # active root set rather than every root ever seen.
    for k in [k for k, v in _doc_ids_cache.items() if v[1] <= now]:
        del _doc_ids_cache[k]
    _doc_ids_cache[cache_key] = (ids, now + _DOC_IDS_TTL_S)
    # hard cap: if still over (churn of distinct fresh roots), drop oldest.
    if len(_doc_ids_cache) > _DOC_IDS_CACHE_CAP:
        for k, _ in sorted(_doc_ids_cache.items(), key=lambda kv: kv[1][1])[
            : len(_doc_ids_cache) - _DOC_IDS_CACHE_CAP
        ]:
            del _doc_ids_cache[k]
    return ids


# ─── system-subtree ancestry guard ──────────────────────────────────────────
#
# The publishability check itself lives in SYSTEM: share-guard (share_guard.py)
# so the mint chokepoint (document_shares) and this resolve funnel share ONE
# implementation. See share_guard.py for the ANCESTRY-not-flag rationale.


async def _find_share_row(document_id: str, project_id: str) -> dict | None:
    """Nearest live share covering document_id, or None if unpublished.

    Walks the ancestor chain (nearest-first, includes document_id at [0]) and
    returns the most specific qualifying row: a direct row on document_id (any
    scope) wins; otherwise the nearest ancestor with scope='subtree'. A
    doc-scope ancestor is skipped — it publishes only itself, not descendants.
    """
    db = await get_db()
    chain = await get_ancestor_ids(document_id, project_id)  # nearest-first
    rows = await db.query(
        "SELECT document_id, scope FROM document_shares "
        "WHERE document_id IN $ids AND deleted_at IS NONE",
        {"ids": chain},
    )
    row_map = {r["document_id"]: r for r in (rows or [])}
    for anc in chain:  # nearest-first: most specific share wins
        r = row_map.get(anc)
        if r is None:
            continue
        if anc == document_id or r["scope"] == "subtree":
            return r
    return None


async def resolve_share(document_id: str) -> dict:
    """The single funnel: document_id → share context (project_id, root, scope, doc_ids).

    # INVARIANT(security): the ONLY read path on this router.  Why: centralizing public-read auth in one path means the share-scope check (document_shares row or subtree ancestor) can't be bypassed by a sibling route. A document is
    # published iff it has a live document_shares row (any scope) OR its nearest
    # qualifying ancestor has scope='subtree'. The agent-config subtree is
    # refused by ancestry BEFORE the share lookup — a pre-guard row on a system
    # doc MUST 404 here. Rejects missing/deleted docs with a uniform 404 (no
    # existence oracle for anonymous callers), then derives doc_ids.

    Returns {project_id, document_id (share ROOT), scope, doc_ids}.
    """
    doc = await fetch_one("documents", document_id)
    if not doc or doc.get("deleted_at"):
        # Uniform 404 — unknown/unpublished/system: indistinguishable.
        raise HTTPException(status_code=404, detail="Document not found")
    project_id = doc.get("project_id")
    if not project_id or not await fetch_one("projects", project_id):
        # Uniform 404 — no project row, or the project is deleted (project delete
        # marks only the project row, the share row survives): indistinguishable
        # from an unknown doc for anonymous callers.
        raise HTTPException(status_code=404, detail="Document not found")
    if await is_unshareable_by_ancestry(document_id, project_id):
        # Uniform 404 — system subtree is unpublishable by ancestry.
        raise HTTPException(status_code=404, detail="Document not found")
    chosen = await _find_share_row(document_id, project_id)
    if not chosen:
        # No direct row, no subtree ancestor → unpublished. Uniform 404.
        raise HTTPException(status_code=404, detail="Document not found")
    root = chosen["document_id"]
    scope = chosen["scope"]
    doc_ids = await _resolve_doc_ids(project_id, root, scope, root)
    return {
        "project_id": project_id,
        "document_id": root,
        "scope": scope,
        "doc_ids": doc_ids,
    }


async def _require_doc(document_id: str) -> dict:
    """resolve_share + assert doc ∈ doc_ids.

    The single guard every read handler calls. There is no other read path.
    Membership is guaranteed by construction (resolve found a share covering
    document_id), but the explicit check stays as a defense-in-depth backstop.
    """
    ctx = await resolve_share(document_id)
    if document_id not in ctx["doc_ids"]:
        # Uniform 404 on out-of-scope — no existence oracle.
        raise HTTPException(status_code=404, detail="Document not found")
    return ctx


async def _token_to_root(token: str) -> str:
    """Resolve a legacy plaintext token to its share-root document_id.

    Kept so the /api/public/{token}/* shim routes can validate the token (404 on
    invalid/deleted, preserving the old contract) and then delegate to the
    canonical document_id handlers. The token is a LEGACY lookup key only — it is
    not the access credential under the re-keyed model (the document_id is).
    """
    db = await get_db()
    rows = await db.query(
        "SELECT document_id FROM document_shares "
        "WHERE token = $t AND deleted_at IS NONE LIMIT 1",
        {"t": token},
    )
    if not rows:
        # Uniform 404 — invalid token, deleted share: indistinguishable.
        raise HTTPException(status_code=404, detail="Share not found")
    return rows[0]["document_id"]


# ─── Handlers (shared by canonical + token-shim routes) ─────────────────────


def _build_public_tree_nodes(rows: list[dict], root_id: str) -> list[dict]:
    """Shape DB rows into public tree nodes, nulling the share root's parent_id.

    INVARIANT(public-tree-root): the share root's node carries parent_id = None on
    the public surface, even when its REAL DB parent exists. Why: the subtree root
    hangs off a project doc that is OUT of the shared subtree by definition. The FE
    buildDocumentTree (document-tree-slice.ts:32-40) keeps a node as a root only
    when parent_id is null and as a child only when parent_id is present in the set
    — a node whose parent is set-but-absent is SILENTLY DROPPED, so the un-normalized
    nested root vanished and the whole docs tab rendered empty. Only the root can
    have an out-of-subtree parent (every descendant's parent is inside the subtree
    by construction of subtree_doc_ids = [root] + descendants), so nulling the root
    alone is the complete fix. WHY NOT fix in buildDocumentTree: adopting orphans as
    roots there would mask genuine orphan bugs on the authed surface (where
    /projects/:id returns all docs and orphans shouldn't exist).
    """
    nodes: list[dict] = []
    for r in (rows or []):
        nid = extract_id(r["id"])
        parent = r.get("parent_id")
        parent_id = None if nid == root_id else (extract_id(parent) if parent else None)
        nodes.append({
            "id": nid,
            "title": r.get("title") or "",
            "parent_id": parent_id,
            "sort_key": r.get("sort_key"),
        })
    return nodes


async def _handle_tree(document_id: str) -> dict:
    """Subtree structure (id/title/parent/sort_key). Doc-scope = root only.

    document_id is the doc the caller navigated to; the tree is always rooted at
    the SHARE root (ctx['document_id']) so the sidebar shows the whole published
    subtree regardless of which descendant the URL names.
    """
    ctx = await resolve_share(document_id)
    db = await get_db()
    rows = await db.query(
        # WHY is_reference = false: references are `documents` rows (is_reference=true,
        # parent_id = owning doc) and thus appear as children in the descendant walk
        # that built `doc_ids`. Without this filter they leak into the public tree and
        # render as document nodes (reported bug). Mirrors the authed tree query
        # (projects.py: "AND is_reference = false"). References have their own panel.
        "SELECT id, title, parent_id, sort_key FROM documents "
        "WHERE project_id = $pid AND deleted_at IS NONE "
        "AND is_reference = false AND meta::id(id) IN $ids ORDER BY sort_key",
        {"pid": ctx["project_id"], "ids": ctx["doc_ids"]},
    )
    # WHY project_name: the public header shows the owning project's name (breadcrumb
    # root). Only the name is disclosed — the subtree content is already public, and
    # no project id / navigation leaks (the anonymous surface has no /projects/ path).
    proj = await db.query(
        "SELECT name FROM projects WHERE meta::id(id) = $pid LIMIT 1",
        {"pid": ctx["project_id"]},
    )
    project_name = (proj[0]["name"] if proj else "") or ""
    nodes = _build_public_tree_nodes(rows, ctx["document_id"])
    return {
        "nodes": nodes,
        "scope": ctx["scope"],
        "root_id": ctx["document_id"],
        "project_name": project_name,
    }


async def _capture_public_content(document_id: str, db_content: str) -> tuple[str, str | None]:
    """Live Y.Doc text + tables JSON for the public reader, or 503.

    Returns ``(content, tables_json)``; ``db_content`` is the documents.content row
    value, used only for the empty-live_text case below.
    """
    try:
        from ydoc_store import capture_live_state
        # WHY: the live Y.Doc text is authoritative, not documents.content.
        # Why: collab/agent writes land in the Y.Doc; documents.content only flushes
        # on autosave and is empty/stale for an agent-created doc — serving it showed
        # a blank public page while the authed editor (reading the Y.Doc) had text.
        live_text, tables_json = await capture_live_state(document_id)
        # WHY: an empty live_text under non-empty documents.content means a persisted
        # ydoc_state decoded to no text — keep the DB fallback, because a blank public
        # page is a worse failure than slightly older text. This is NOT the legacy path:
        # a doc that never entered collab has no ydoc_state and load() seeds it FROM
        # documents.content, so live_text already equals it.
        return (live_text or db_content), tables_json
    except Exception as e:  # noqa: BLE001 — refuse the read, see the INVARIANT below.
        # INVARIANT(data-loss): a capture_live_state throw serves nothing, never
        # documents.content. Why: load() seeds from documents.content when ydoc_state
        # is absent and from "" when the row is missing, so it never raises for "no
        # live state" — the only meaning left is Surreal/CRDT-decode breakage, and
        # falling back there would show a reader content lagging the live doc by every
        # uncompacted ydoc_updates edit (CLAUDE.md: no silent degradation).
        # The structured log key is the signal: telemetry_event would fail with Surreal.
        logger.warning("capture_live_state failed for public doc %s: %r", document_id, e)
        raise HTTPException(
            status_code=503, detail="Document temporarily unavailable"
        ) from e


async def _handle_document(document_id: str) -> dict:
    """Document content + server-derived headings (TOC source) + tables_json.

    `tables_json` captures the live Yjs tables subtree so the public Editor's
    table widgets render the same chrome as the authed editor (plan
    "public-share-reuse-readonly-layout" Risks: "if tables need tables_json,
    extend publicDocument to return it"). A doc with no tables yields ``"{}"``;
    a live-state capture failure is a 503, not a degraded payload.
    """
    await _require_doc(document_id)
    db = await get_db()
    rows = await db.query(
        "SELECT id, title, content FROM documents "
        "WHERE id = type::record('documents', $id) AND deleted_at IS NONE",
        {"id": document_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Document not found")
    row = rows[0]
    content, tables_json = await _capture_public_content(document_id, row.get("content") or "")
    return {
        "document_id": document_id,
        "title": row.get("title") or "",
        "content": content,
        "tables_json": tables_json,
        "headings": extract_headings(content),
    }


async def _handle_references(document_id: str) -> dict:
    """All subtree references (with content), sorted depth-tier by the current doc.

    Scope is the WHOLE shared subtree, not doc+ancestors. Why: public
    transclusion seeds inline `ref:` embeds ONLY from this payload —
    `buildPublicTransclusionMap` reads `ref.content` straight off the store
    `references`, and `usePublicTransclusionSync` has no lazy ref fetch (the
    anonymous router has no per-ref GET; the authed one would 401). A doc may
    embed a ref owned by ANY subtree node, so every subtree ref body must ship
    here, or its embed renders permanently broken. (An earlier "editor parity"
    re-scope to doc+ancestors broke exactly this — regression test
    `test_public_references_carry_descendant_ref_body_for_transclusion`. The
    authed surface can afford doc+ancestors scope because it lazy-fills gaps via
    `GET /references/{id}`; the public surface cannot.)

    Sort is depth-tier relative to the current doc: own refs first, then
    ancestors by proximity, then the rest of the subtree, newest updated_at DESC
    within each tier — via the shared `sort_refs_by_depth_tier`. The single
    membership boundary is still `resolve_share` + `doc ∈ doc_ids`: refs owned by
    docs OUTSIDE the subtree never appear.
    """
    ctx = await _require_doc(document_id)
    db = await get_db()
    # Sort tier key (NOT the query scope): current doc + its in-subtree ancestors.
    # Own refs tier 0, ancestors by proximity; out-of-subtree ancestors (above the
    # share root) are excluded — they hold no in-scope refs anyway. Used only by
    # the depth-tier sort below; the QUERY reads the whole subtree (transclusion).
    full_ancestors = await get_ancestor_ids(document_id, ctx["project_id"])
    scope_set = set(ctx["doc_ids"])
    sort_tier_ids = [a for a in full_ancestors if a in scope_set]
    # WHY SELECT * (not _REF_META_SELECT): public transclusions render inline
    # on /docs/<id> from the data already in this response. The authed LIST
    # strips `content` via column projection (`_REF_META_SELECT` in
    # references.py), NOT via the serializer — `serialize_ref_meta`'s docstring
    # advertises "metadata-only, no content" but the function itself passes
    # `content` through unchanged. The public path RELIES on that passthrough so
    # the FE `buildPublicTransclusionMap` can seed ref-text entries from this
    # payload without a lazy authed fetch (which would 401 anonymously). If a
    # future "make the serializer match its docstring" change drops `content`
    # here, public transclusions silently break with no failing test — guarded
    # by tests/backend/test_public_share.py
    # `test_public_references_carry_content_for_transclusion`.
    rows = await db.query(
        "SELECT * FROM documents WHERE is_reference = true AND deleted_at IS NONE "
        "AND archived != true AND parent_id IN $ids",
        {"ids": ctx["doc_ids"]},
    )
    refs = dedup_refs_by_id(rows)
    return {"references": sort_refs_by_depth_tier(refs, sort_tier_ids)}


async def _handle_file_thumbnail(document_id: str, reference_id: str):
    """Serve an image-ref thumbnail anonymously, generating on demand.

    Single funnel (INVARIANT): resolve_share + the ref's owning doc (parent_id)
    ∈ doc_ids. No `get_current_user` — anonymous. Reuses the authed
    `_serve_thumbnail` body so the generate-on-demand + immutable Cache-Control
    are identical to `/api/files/{id}/thumb`.

    ROUTE ORDERING INVARIANT: the {document_id}/files/{reference_id}/thumb
    handler is declared BEFORE the generic .../{filename} handler so the literal
    `thumb` segment wins. If reordered, FastAPI would match `thumb` as {filename}  Why: FastAPI matches routes in declaration order, so the literal `thumb` segment must be declared before the {filename} catch-all or it is swallowed as a filename.
    and serve the full-size image (bandwidth) or 404 on the missing `thumb` file.

    ARCH(no-rate-limit) reconciliation: this route *inherits* the no-rate-limit
    decision. Thumbnail generation runs PIL on cold requests but is bounded by
    the write-once disk cache (each unique image generated once) + the small
    per-share image count, so the existing four-point rationale still holds. Why
    pin: a reviewer seeing "anonymous endpoint runs PIL" will reflexively want a
    rate limiter, re-introducing the 429-on-honest-readers harm.
    """
    ctx = await resolve_share(document_id)
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        # Uniform 404 — no existence oracle for anonymous callers.
        raise HTTPException(status_code=404, detail="Reference not found")
    parent_id = extract_id(ref.get("parent_id")) if ref.get("parent_id") else None
    if not parent_id or parent_id not in ctx["doc_ids"]:
        # The ref's owning doc is out of scope — uniform 404 (no existence oracle).
        raise HTTPException(status_code=404, detail="Reference not found")
    return await _serve_thumbnail(ref, reference_id)


async def _handle_file(document_id: str, reference_id: str, filename: str):
    """Serve a stored reference file anonymously.

    The single funnel extends to file serving (INVARIANT): resolve the share,
    load the ref, assert its OWNING doc (parent_id) ∈ doc_ids. No
    `get_current_user` — anonymous. Reuses `_serve_reference_file` so the
    traversal guard + immutable Cache-Control are identical to the authed route.

    Path-traversal is guarded twice: the share's doc_ids check rejects ref_ids
    whose owning doc is out of scope, and `_serve_reference_file` rejects
    file_path values that escape STORAGE_PATH.
    """
    ctx = await resolve_share(document_id)
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        # Uniform 404 — no existence oracle for anonymous callers.
        raise HTTPException(status_code=404, detail="Reference not found")
    parent_id = extract_id(ref.get("parent_id")) if ref.get("parent_id") else None
    if not parent_id or parent_id not in ctx["doc_ids"]:
        # The ref's owning doc is out of scope — uniform 404 (no existence oracle).
        raise HTTPException(status_code=404, detail="Reference not found")
    return await _serve_reference_file(ref, filename)


# ─── Canonical routes: /api/public/documents/{document_id}/* ────────────────


@router.get("/api/public/documents/{document_id}/tree")
async def public_tree(document_id: str):
    return await _handle_tree(document_id)


@router.get("/api/public/documents/{document_id}")
async def public_document(document_id: str):
    return await _handle_document(document_id)


@router.get("/api/public/documents/{document_id}/references")
async def public_references(document_id: str):
    return await _handle_references(document_id)


@router.get("/api/public/documents/{document_id}/files/{reference_id}/thumb")
async def public_file_thumbnail(document_id: str, reference_id: str):
    # ROUTE ORDERING INVARIANT (see _handle_file_thumbnail): declared before the
    # generic {filename} route below so the literal `thumb` segment wins.
    return await _handle_file_thumbnail(document_id, reference_id)


@router.get("/api/public/documents/{document_id}/files/{reference_id}/{filename}")
async def public_file(document_id: str, reference_id: str, filename: str):
    return await _handle_file(document_id, reference_id, filename)


# ─── Legacy token shims: /api/public/{token}/* (HTTP 200 alias) ──────────────
#
# ARCH(plan "public-document-ids"): the token is no longer the URL segment. These
# routes are retained as thin alias shims so any client/external integration still
# holding a /api/public/{token}/* URL keeps working: they validate the token
# (404 on invalid/deleted — preserving the old contract) then delegate to the
# canonical document_id handlers above. They return HTTP 200 with the SAME body
# (not a 301): page-level URLs (/s/:token, /projects/:pid/docs/:did) are what
# redirect to /docs/<id> (plan Redirects, step 6); internal API calls alias.
#
# For routes whose path carries {document_id} (content/refs), the token is
# validated and the path document_id is delegated (resolve_share is document-keyed
# now). For routes without {document_id} (tree/files), the token resolves to the
# share ROOT, which is delegated.


@router.get("/api/public/{token}/tree")
async def legacy_public_tree(token: str):
    root = await _token_to_root(token)
    return await _handle_tree(root)


@router.get("/api/public/{token}/documents/{document_id}")
async def legacy_public_document(token: str, document_id: str):
    await _token_to_root(token)  # validate token (uniform 404 on invalid/deleted)
    return await _handle_document(document_id)


@router.get("/api/public/{token}/documents/{document_id}/references")
async def legacy_public_references(token: str, document_id: str):
    await _token_to_root(token)
    return await _handle_references(document_id)


@router.get("/api/public/{token}/files/{reference_id}/thumb")
async def legacy_public_file_thumbnail(token: str, reference_id: str):
    root = await _token_to_root(token)
    return await _handle_file_thumbnail(root, reference_id)


@router.get("/api/public/{token}/files/{reference_id}/{filename}")
async def legacy_public_file(token: str, reference_id: str, filename: str):
    root = await _token_to_root(token)
    return await _handle_file(root, reference_id, filename)
