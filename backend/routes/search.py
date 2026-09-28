"""Semantic search routes — public vector search API for agents and UI."""
# SYSTEM: semantic-search — public vector search API for agents and UI
# ARCH: Decoupled from chat — reusable by any consumer (agents, frontend, future tools).
# ARCH: Dual auth — cookie session (frontend) OR Bearer API key (agents), resolved via optional dependency.

import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request

from access import get_project_access
from auth import get_current_user
from db import fetch_one
from routes.api_keys import get_api_key_context

router = APIRouter()
logger = logging.getLogger(__name__)


async def _optional_current_user(
    request: Request,
    lore_session: Optional[str] = Cookie(default=None),
) -> Optional[dict]:
    """get_current_user that yields None (not 401) when no cookie is present.

    Why: this route's declared auth is dual (cookie OR Bearer API key), but the
    plain get_current_user dependency raises 401 before the handler ever runs,
    which made the Bearer half of _resolve_project_access unreachable. Absent
    cookie ⇒ None here; a PRESENT-but-invalid cookie still raises (do not
    silently fall through to the key on a bad cookie); neither credential
    resolving is the handler's 401.
    """
    if not lore_session:
        return None
    return await get_current_user(request, lore_session)


async def _optional_api_key_ctx(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    """API key auth that gracefully returns None when no header is present."""
    if not authorization:
        return None
    return await get_api_key_context(authorization)


async def _resolve_project_access(
    project_id: str,
    user: Optional[dict],
    api_key_ctx: Optional[dict],
) -> dict:
    """Resolve auth context from either cookie or API key.

    Returns a dict with user_id and confirmed project_id.
    Raises 401/403 on auth failures.
    """
    if user:
        access = await get_project_access(project_id, user)
        if not access:
            raise HTTPException(status_code=403, detail="No access to this project")
        return {"user_id": user["user_id"], "project_id": project_id}

    if api_key_ctx:
        if api_key_ctx["project_id"] != project_id:
            raise HTTPException(status_code=403, detail="API key not scoped to this project")
        return api_key_ctx

    raise HTTPException(status_code=401, detail="Authentication required")


@router.get("/api/projects/{project_id}/semantic-search")
async def semantic_search(
    project_id: str,
    q: str = Query(..., min_length=2),
    k: int = Query(8, ge=1, le=50),
    top_k_docs: Optional[int] = Query(None, ge=1, le=100),
    top_k_refs: Optional[int] = Query(None, ge=1, le=100),
    include_docs: bool = Query(True),
    include_refs: bool = Query(True),
    # A third switch for the third kind (SYSTEM: memory). Defaults ON and is
    # INDEPENDENT of include_docs — an entity-doc is is_reference=false, so folding it
    # into the documents switch would make "search the references only" also mean
    # "forget everything the project established".
    include_memory: bool = Query(True),
    under_document_id: Optional[str] = Query(None),
    user: Optional[dict] = Depends(_optional_current_user),
    api_key_ctx: Optional[dict] = Depends(_optional_api_key_ctx),
):
    """Semantic vector search over doc_chunks (documents, reference-documents, memory).

    Returns scored hits with parent metadata, snippets, and offsets. A `memory` hit is
    a whole project-memory entity (see SYSTEM: memory) and is ranked ahead of
    chunk-level hits.
    Requires read access to the project (cookie auth or API key).
    """
    ctx = await _resolve_project_access(project_id, user, api_key_ctx)

    # Subtree narrow — the same contract the agent executor applies
    # (search_exec ARCH(under_document_id)); kept literal there and here (under
    # the extract threshold), so change BOTH or neither.
    # WHY: the key's scope (scope_root on a Bearer key ctx) is a CEILING — a
    # requested narrow may only narrow further (key_subtree ∩ requested), never
    # widen. An unknown/cross-project/deleted root is a uniform 404 (no
    # existence oracle); out-of-scope is a 403 naming the remedy.
    scope_root = (ctx.get("scope_root") or "").strip() or None
    allowed: Optional[set] = None
    if scope_root:
        from scope import subtree_doc_ids

        allowed = set(await subtree_doc_ids(scope_root, ctx["project_id"]))
    under = (under_document_id or "").strip() or None
    if under:
        from scope import in_subtree, out_of_scope_detail, subtree_doc_ids

        root_row = await fetch_one("documents", under)
        if (
            not root_row
            or root_row.get("deleted_at")
            or root_row.get("project_id") != ctx["project_id"]
        ):
            raise HTTPException(status_code=404, detail="Search root document not found")
        if scope_root and not await in_subtree(scope_root, under):
            raise HTTPException(status_code=403, detail=out_of_scope_detail(scope_root))
        requested = set(await subtree_doc_ids(under, ctx["project_id"]))
        # Intersect, never replace; an empty intersection is an ERROR — coercing
        # it to `None` would silently turn an impossible narrow into a
        # whole-project search.
        allowed = requested if allowed is None else allowed & requested
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "The requested subtree holds no documents inside this "
                    f"key's subtree scope (root {scope_root}). Retry with an "
                    "under_document_id inside that scope."
                ),
            )

    try:
        from retrieval import retrieve_context
    except Exception as e:
        logger.warning("Failed to import retrieval module: %s", e)
        raise HTTPException(status_code=503, detail="Semantic search not available")

    from config import RETRIEVAL_BUDGET_TOKENS_DOCS, RETRIEVAL_BUDGET_TOKENS_REFS

    # ARCH: k is the default per-kind limit; top_k_docs/top_k_refs override per-kind when set.
    # Agent use case: ?k=3 → narrow lookup (3 candidates per kind before filtering).
    #                 ?k=50 → broad exploration (50 candidates, then MIN_SCORE + DROP-OFF trim).
    #                 ?top_k_docs=20&top_k_refs=5 → asymmetric (lots of docs, few refs).
    # Memory follows `k` as well — it is a kind, and a per-kind limit that silently
    # ignored one kind would make ?k=50 mean "50 of each, except memory".
    effective_top_k_docs = top_k_docs if top_k_docs is not None else k
    effective_top_k_refs = top_k_refs if top_k_refs is not None else k

    result = await retrieve_context(
        project_id=ctx["project_id"],
        user_query=q,
        history=[],
        include_documents=include_docs,
        include_references=include_refs,
        include_memory=include_memory,
        token_budget_docs=RETRIEVAL_BUDGET_TOKENS_DOCS,
        token_budget_refs=RETRIEVAL_BUDGET_TOKENS_REFS,
        top_k_docs=effective_top_k_docs,
        top_k_refs=effective_top_k_refs,
        top_k_memory=k,
        # None = whole project (the INVARIANT default); a set = the validated
        # subtree narrow, applied in SQL (pushdown) with memory scoped by
        # provenance inside retrieve_context.
        allowed_doc_ids=allowed,
    )

    if result.error:
        if result.error == "not_configured":
            raise HTTPException(status_code=503, detail="Embeddings not configured")
        if result.error == "embedding_failed":
            raise HTTPException(status_code=502, detail="Embedding service error")
        raise HTTPException(status_code=500, detail=result.error)

    return {
        "query": q,
        "hits": [
            {
                "kind": h.kind,
                "parent_id": h.parent_id,
                "parent_title": h.parent_title,
                "heading": h.heading,
                "snippet": h.snippet,
                "offset_start": h.offset_start,
                "offset_end": h.offset_end,
                "score": round(h.score, 4),
                # The references a memory fact was distilled from; empty on every
                # chunk-level hit (see the RetrievalHit INVARIANT(journal)).
                "sources": h.sources,
            }
            for h in result.hits
        ],
    }
