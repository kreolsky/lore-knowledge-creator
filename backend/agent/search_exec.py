"""Agent read-only search executor (auto-run inside the loop / Tool-API).

Part of the chat-agent-mode read-only tool surface. Extracted from
readonly_executors — internal helper module: the public wrapper
`search_materials_tool` is re-exported from there so every existing importer keeps
its path. The live surfaces (Tool-API reads.py, MCP dispatch.py) call this wrapper
directly and forward `mode` themselves.
"""
import logging

import settings
from fastapi import HTTPException

# The stop-word set, stop-word stripper and snippet-anchor ladder live in
# textmatch.py (shared with the UI search route — agent layering must not be
# imported from routes, so the shared code sits at the backend root). Re-imported
# under private aliases: these names are part of this module's tested
# surface (tests/backend/test_search_exec_units.py imports them from here).
from textmatch import (
    RU_STOP_WORDS as _RU_STOP_WORDS,  # noqa: F401 — re-export pinning the moved name
)
from textmatch import (
    snippet_anchor as _snippet_pos,
)
from textmatch import (
    strip_stop_words as _strip_stop_words,
)

from access import get_document_access
from db import extract_id, fetch_one, get_db
from models import is_ref_row

logger = logging.getLogger(__name__)


def _rrf_merge(direct: list[dict], semantic: list[dict], k_const: int = 60) -> list[dict]:
    """Fuse two ranked lists by reciprocal rank: score = Σ 1/(k_const + rank).

    # WHY: the two layers rank on incomparable evidence — the direct layer on a name-match
    # tier, the semantic layer on cosine similarity — so any score-space merge silently
    # ranks by whichever layer happens to be more generous. RRF needs only the ORDER each
    # layer already produced. k_const=60 is the standard damping constant; it flattens the
    # head so neither layer can monopolize it.

    Direct wins label conflicts (a memory fact's `kind` comes from the direct layer
    — see the INVARIANT in the direct loop), so it is folded first and a doc found
    by both is labelled `source: "both"` without re-deriving its kind.
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict] = {}
    for rank, h in enumerate(direct, start=1):
        did = h["doc_id"]
        scores[did] = scores.get(did, 0.0) + 1.0 / (k_const + rank)
        if did not in meta:
            meta[did] = dict(h)
    for rank, h in enumerate(semantic, start=1):
        did = h["doc_id"]
        scores[did] = scores.get(did, 0.0) + 1.0 / (k_const + rank)
        if did not in meta:
            meta[did] = dict(h)
        else:
            meta[did]["source"] = "both"
    # Order by RRF score desc; doc_id is a deterministic tiebreak.
    ordered = sorted(meta, key=lambda d: (-scores[d], d))
    # INVARIANT: this order is the ONLY ranking signal a hit carries — the builders emit
    # no `score`, and nothing may add one here.
    # Why: any per-hit number comes from one layer while this order is computed from both,
    # so a consumer sorting by it undoes the fusion — and the direct layer's band was a
    # constant 0.5 on every multi-word query (name_match_tier matches the WHOLE query as a
    # substring), which read to a weak model as "no hit is more relevant than any other".
    return [meta[d] for d in ordered]


async def _resolve_under_root(
    project_id: str, scope_root: str | None, under: str,
) -> None:
    """Validate ONE typed subtree root as a live in-scope id, or raise.

    Existence + same-project first (uniform 404, no existence oracle — same
    policy as gate_mutation_target), then the key's ceiling: a real
    out-of-scope id is a permission answer (403 naming the remedy), not a
    spelling one.
    """
    from scope import in_subtree, out_of_scope_detail

    root_row = await fetch_one("documents", under)
    if (
        not root_row
        or root_row.get("deleted_at")
        or root_row.get("project_id") != project_id
    ):
        raise HTTPException(404, "Search root document not found")
    if scope_root and not await in_subtree(scope_root, under):
        raise HTTPException(403, out_of_scope_detail(scope_root))


async def _intersect_scope_ceiling(
    scope_root: str | None, allowed: set[str] | None, under: str, project_id: str,
) -> set[str]:
    """Fold the requested subtree into the key's scope ceiling.

    # INVARIANT(security): the key's scope is the ceiling — intersect, never replace. An
    # empty intersection is an ERROR. Why: coercing it to the `allowed = None`
    # "do not filter" sentinel would silently turn an impossible narrow into a
    # whole-project search.
    """
    from scope import out_of_scope_detail, subtree_doc_ids

    requested = set(await subtree_doc_ids(under, project_id))
    allowed = requested if allowed is None else allowed & requested
    if not allowed:
        # scope.out_of_scope_detail(empty_intersection=True) is the ONE producer
        # of this text (no hand-written near-copy — the recovery tail cannot
        # drift between the doc-target and search-narrow variants).
        raise HTTPException(403, out_of_scope_detail(scope_root, empty_intersection=True))
    return allowed


async def _resolve_search_scope(
    project_id: str, scope_root: str | None, under_document_id: str | None,
) -> set[str] | None:
    """Resolve the search narrow: the `allowed` subtree id set.

    None ⇒ whole project; a set ⇒ subtree only. Raises the narrow's 404/403
    contract (uniform 404, scope ceiling 403, empty-intersection 403 naming
    the remedy).

    # WHY: search covers the WHOLE project by default; it narrows to a
    # subtree ONLY when explicitly asked. Why: a default subtree narrow
    # silently drops project memory (`.lore/system/memory` is outside every
    # user subtree), and a model that cannot see what it is missing reports
    # absence instead of widening.
    """
    from scope import subtree_doc_ids

    allowed: set[str] | None = None
    if scope_root:
        allowed = set(await subtree_doc_ids(scope_root, project_id))
    under = (under_document_id or "").strip() or None
    if under:
        await _resolve_under_root(project_id, scope_root, under)
        allowed = await _intersect_scope_ceiling(
            scope_root, allowed, under, project_id,
        )
    return allowed


def _build_direct_hit(r: dict, *, snippet: str) -> dict:
    """One direct-layer hit, shaped for the fusion input. The kind label is
    authoritative here — see the memory-labelling note in the direct loop."""
    # A memory fact is `is_reference=false`, so the two-way label would call it
    # a document here — and the direct layer runs FIRST, so that hit enters
    # `seen` and suppresses the correctly-labelled semantic one. The fact would
    # then reach the model as an ordinary document on exactly the docs whose
    # title matches the query.
    is_mem = bool(r.get("is_memory"))
    kind = "memory" if is_mem else ("reference" if is_ref_row(r) else "document")
    parent_raw = r.get("parent_id")
    return {
        "kind": kind,
        "doc_id": r["id"],
        "parent_id": extract_id(parent_raw) if parent_raw else None,
        "title": r.get("title") or "",
        "heading": None,
        "snippet": snippet,
        "source": "direct",
    }


async def _union_candidate_rows(
    db, *, select_cands: str, contains_pred: str,
    project_id: str, query: str, scope_params: dict, warnings: list[str],
) -> list:
    """Semantic mode's FTS∪CONTAINS union scan, with the CONTAINS fallback.

    # INVARIANT: search never returns empty because the FTS index is absent —
    # fall back to the CONTAINS scan (mirrors search_documents in
    # routes/projects.py). Why: apply_schema swallows a DDL failure, so a missing
    # index is a realistic node state, not an edge case. The fallback DEGRADES
    # recall (the stemmer's inflected matches are gone) and says so in
    # `warnings` — the module's own contract and both layer catches report
    # themselves the same way; a CONTAINS-only recall that reads as full recall
    # is exactly the silent degradation this surface bans.
    """
    # `$qfts` is the stop-word-stripped query — SurrealDB FULLTEXT matches ANY
    # token (OR), so a natural-language query fan-outs to every doc sharing a
    # function word. Stripping cuts `'что…владельца'` 111→6 on the dev project
    # (S1.0). Falls back to the raw query when stripping empties it, so a query
    # of only stop words still searches.
    qfts = _strip_stop_words(query) or query
    try:
        cand_rows = await db.query(
            select_cands
            + "AND ((title @0@ $qfts OR content @1@ $qfts) "
            "OR string::lowercase(title ?? '') CONTAINS string::lowercase($q) "
            "OR string::lowercase(content ?? '') CONTAINS string::lowercase($q)) "
            "LIMIT 200",
            {"pid": project_id, "q": query, "qfts": qfts, **scope_params},
        )
        if isinstance(cand_rows, str):
            raise RuntimeError(cand_rows)
        return cand_rows
    except Exception as fts_err:
        logger.warning(
            "search_materials FTS disjunct failed, falling back to CONTAINS: %s",
            fts_err,
        )
        warnings.append("search_degraded:fts")
        return await db.query(
            select_cands + contains_pred + "LIMIT 200",
            {"pid": project_id, "q": query, **scope_params},
        )


def _candidate_sql(allowed: set[str] | None) -> tuple[str, dict]:
    """The candidate-pool SELECT + its subtree-scope params (T5: scope the pool
    to the subtree when scoped). Content rides along — the snippet and the
    content-tier scoring need it."""
    scope_clause = (
        "AND meta::id(id) IN $allowed " if allowed is not None else ""
    )
    scope_params: dict = {"allowed": list(allowed)} if allowed is not None else {}
    select_cands = (
        "SELECT meta::id(id) AS id, title, content, is_reference, parent_id, "
        "is_memory, updated_at, sort_key FROM documents "
        "WHERE project_id = $pid AND deleted_at IS NONE "
        + scope_clause
    )
    return select_cands, scope_params


def _contains_pred() -> str:
    """The CONTAINS disjunct — the exact path AND the union's fallback. `?? ''`
    guards the NONE-content row: content is option<string>, and
    string::lowercase(NONE) raises InternalError; the layer-wide except would
    then disable the whole lexical layer for one empty doc (F7 — a live
    failure, not a hypothesis)."""
    return (
        "AND (string::lowercase(title ?? '') CONTAINS string::lowercase($q) "
        "OR string::lowercase(content ?? '') CONTAINS string::lowercase($q)) "
    )


async def _exact_candidate_rows(
    db, *, select_cands: str, contains_pred: str,
    project_id: str, query: str, scope_params: dict,
) -> list:
    """exact = the literal-substring scan alone. Cheaper than the union (no FTS)
    and gives full literal recall: every doc containing the string is a
    candidate, ranked by rank_rows. It is a strict subset of semantic at the
    MATCHER level (CONTAINS ⊆ CONTAINS∪FTS) — the property the D9a contract
    rests on. (Top-k slices can differ when literal matches outnumber k; that
    is a slicing artifact, not a recall-surface the agent can grind on — empty
    exact still means the string is absent from the project.)

    # INVARIANT: `exact` may never gain a matcher the union lacks (D9a). Why:
    # it is the whole reason re-running a failed `exact` search in other words
    # is a provable waste rather than a plausible one, which is what the tool
    # description and the zero-hit warning tell the agent.
    """
    return await db.query(
        select_cands + contains_pred + "LIMIT 200",
        {"pid": project_id, "q": query, **scope_params},
    )


# ARCH(corpus): the model-facing WHAT-switch, one value matching the entity
# model (references = raw stock, documents = compiled artifact, memory =
# distilled knowledge). Threaded as the single `corpus` value through the
# executor and both layers; marshalled to the retrieval-layer flags
# (include_documents / include_references / include_memory — themselves
# unchanged) at the ONE retrieve_context boundary. Replaces the old
# include_docs/include_refs booleans whose all-off residue was "memory only".
# Memory gating changed WITH the switch: the old gate passed a memory fact
# regardless of the booleans ("narrowing the raw corpus is not a request to
# forget"), but corpus:"documents"/"references" IS a request for one raw kind
# alone — memory drops with the other kinds, and `all` keeps today's behavior
# by construction instead of by comment.
CORPUS_VALUES: tuple[str, ...] = ("all", "memory", "documents", "references")
_CORPUS_FLAGS: dict[str, tuple[bool, bool, bool]] = {
    # corpus: (include_documents, include_references, include_memory)
    "all": (True, True, True),
    "memory": (False, False, True),
    "documents": (True, False, False),
    "references": (False, True, False),
}


def _filter_by_kind(rows, *, corpus: str) -> list[dict]:
    """Keep rows the corpus allows (direct-layer rows carry is_memory /
    is_reference). Kind labelling stays on every hit — labelling what was found
    is useful, choosing it blind is not; `corpus` is the chooser."""
    include_docs, include_refs, keep_memory = _CORPUS_FLAGS[corpus]
    kind_filter = []
    if include_docs:
        kind_filter.append(False)
    if include_refs:
        kind_filter.append(True)
    return [
        r
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict) and (
            # A memory row is gated by keep_memory ALONE — falling through to
            # the is_ref_row check would pass it as a document under
            # corpus="documents" (is_ref_row False ∈ [False]).
            (keep_memory if r.get("is_memory") else is_ref_row(r) in kind_filter)
        )
    ]


async def _lexical_candidate_rows(
    db, *, project_id: str, query: str, qlower: str, exact: bool,
    allowed: set[str] | None, corpus: str,
    warnings: list[str],
) -> list[dict]:
    """The direct layer's candidate pool: a DB-side title/content scan.

    `exact` runs the CONTAINS predicate alone; semantic mode runs the FTS∪CONTAINS
    union (the two matchers are ORed, never swapped — neither contains the other,
    and a missing FTS index degrades RANKING, never RESULTS) and falls back to
    CONTAINS on an FTS failure, appending `search_degraded:fts` (the stemmer's
    inflected matches are then gone from recall — the model is told, never left
    to infer).
    """
    if not qlower:
        return []
    select_cands, scope_params = _candidate_sql(allowed)
    contains_pred = _contains_pred()
    if exact:
        cand_rows = await _exact_candidate_rows(
            db, select_cands=select_cands, contains_pred=contains_pred,
            project_id=project_id, query=query, scope_params=scope_params,
        )
    else:
        cand_rows = await _union_candidate_rows(
            db, select_cands=select_cands, contains_pred=contains_pred,
            project_id=project_id, query=query, scope_params=scope_params,
            warnings=warnings,
        )
    return _filter_by_kind(cand_rows, corpus=corpus)


def _build_direct_hits(ranked: list[dict], *, qlower: str) -> list[dict]:
    """Turn ranked candidate rows into direct-layer hits, budgeted.

    A fact is served WHOLE at both layers: its body IS the fact (see
    SYSTEM: memory), so a 180-char window around the query hands back a
    fragment of a statement that was written to be read entire.
    """
    direct: list[dict] = []
    for r in ranked:
        body = r.get("content") or ""
        # S1.2: the match may be a stem/prefix form whose query string is not
        # verbatim in the body — window around the best available anchor instead
        # of always falling back to the document head (which reads as a wrong hit).
        pos = _snippet_pos(body.lower(), qlower)
        snippet = body[max(0, pos - 60) : pos + 120] if pos >= 0 else body[:160]
        if r.get("is_memory") and body:
            snippet = body
        direct.append(_build_direct_hit(r, snippet=snippet))
    return direct


async def _direct_lexical_layer(
    db, *, project_id: str, query: str, qlower: str, exact: bool,
    allowed: set[str] | None, corpus: str,
    k: int, warnings: list[str],
) -> list[dict]:
    """Layer A — direct DB title/substring match over the real project tree.

    Two queries: a DB-side title/content candidate pool with a wide LIMIT
    (WITH content — snippet + content-tier scoring), then Python ranking via
    doc_proximity.rank_rows. A layer failure appends `search_degraded:direct`
    and yields [] — never a raise out of the layer.

    # ARCH: NO whole-project parent-map scan runs on
    # search — rank_rows is no-anchor by design (see the orchestrator's
    # ARCH note), and tree_distance only consults a parent map when an anchor
    # exists. If an anchor is ever wired (the recoverable primitive on
    # rank_rows), the wiring brings the map scan back WITH it.
    """
    from doc_proximity import rank_rows

    try:
        candidates = await _lexical_candidate_rows(
            db, project_id=project_id, query=query, qlower=qlower, exact=exact,
            allowed=allowed, corpus=corpus,
            warnings=warnings,
        )
        ranked = rank_rows(
            candidates,
            anchor_id=None,  # ARCH: no-anchor by design — see the orchestrator's note
            query=qlower,
            parent_map=None,
        )
        # WHY: rank_rows tiers by substring (name_match_tier), which is
        # intentionally STRICTER than the candidate query — a stem-only hit
        # (matched by FTS but not by any literal substring) lands in the default
        # tier (0.5) while a literal title match keeps 1.0 (D3). Do not "fix" the
        # tiering to match the FTS result set — that would flatten the only
        # signal that orders the head. The budget cap keeps the access-check
        # fan-out bounded on a broad/empty-ish query.
        direct_budget = max(k, await settings.get("AGENT_SEARCH_DIRECT_K")) * 2
        return _build_direct_hits(ranked[:direct_budget], qlower=qlower)
    except Exception as e:
        logger.warning("search_materials direct layer failed: %s", e)
        warnings.append("search_degraded:direct")
        return []


def _build_semantic_hit(h) -> dict:
    """One semantic-layer hit, shaped for the fusion input."""
    return {
        "kind": h.kind,
        "doc_id": h.parent_id,
        "parent_id": h.parent_id,
        "title": h.parent_title,
        "heading": h.heading,
        "snippet": h.snippet,
        "source": "semantic",
        # WHY(journal): a memory hit rides with the references it
        # was distilled from.
        # Why: the model is the consumer that has to judge the fact,
        # and it cannot check an origin it was never shown. (The
        # `unverified` marker is retired, DEC1 — a fact carries no
        # verification state; see `_create_fact`.)
        "sources": h.sources,
    }


def _dedupe_semantic_hits(hits, allowed: set[str] | None) -> list[dict]:
    """Dedupe + subtree-post-filter the semantic layer's chunk hits.

    Dedupe within the layer: retrieve_context returns up to
    RETRIEVAL_MAX_PER_DOC chunk hits per document, so without this a doc would
    be folded into RRF N times and inflate its own score. First occurrence wins
    (rows arrive score-desc, so it is the best chunk). Cross-layer overlap is
    INTENTIONALLY kept — a doc in both direct and semantic is the strongest
    signal and earns source="both" in RRF.

    T5: drop doc/reference hits outside the subtree (no body leak). KIND-AWARE:
    a `memory` hit is exempt — its fact-doc is never inside `allowed` by tree
    position, and the provenance filter already ran inside retrieve_context,
    so a blanket `parent_id not in allowed` here would re-drop every memory hit
    the narrow just rescued (the one-line bug of the provenance plan).
    """
    semantic: list[dict] = []
    sem_seen: set[str] = set()
    for h in hits:
        if allowed is not None and h.kind != "memory" and h.parent_id not in allowed:
            continue
        if h.parent_id in sem_seen:
            continue
        sem_seen.add(h.parent_id)
        # NOTE: do NOT skip docs the direct layer already found — RRF merges
        # the two by rank, and a doc found by BOTH is the strongest signal (it
        # gets source="both" in _rrf_merge). The old concat put every direct
        # hit first and buried this signal.
        semantic.append(_build_semantic_hit(h))
    return semantic


async def _retrieve_semantic_result(
    *, project_id: str, query: str, history: list | None,
    corpus: str, k: int,
    allowed: set[str] | None,
):
    """One retrieve_context call with the layer's budgets and the narrow.

    # INVARIANT(security): under a narrow, memory is filtered by PROVENANCE, never by
    # tree position. Why: fact-docs live flat under the project's memory
    # space, outside every user subtree, so a tree-position filter (the old
    # blanket post-filter) kills EVERY memory hit under any narrow while a
    # provenance filter keeps exactly the facts the subtree's own material
    # produced — retrieve_context applies it in SQL (allowed_doc_ids +
    # _allowed_fact_ids), so a memory hit reaching the merge is in-scope by
    # construction.

    # ARCH(corpus): the ONE marshalling point to the retrieval-layer flags
    # (_CORPUS_FLAGS) — docs/refs off for `memory`, include_memory off for the
    # single-raw-kind values, so the kind gate runs in SQL here and no second
    # post-filter layer can drift against it.
    """
    from config import (
        RETRIEVAL_BUDGET_TOKENS_DOCS,
        RETRIEVAL_BUDGET_TOKENS_REFS,
    )
    from retrieval import retrieve_context

    include_docs, include_refs, include_memory = _CORPUS_FLAGS[corpus]
    return await retrieve_context(
        project_id=project_id,
        user_query=query,
        history=list(history) if history else [],
        include_documents=include_docs,
        include_references=include_refs,
        include_memory=include_memory,
        token_budget_docs=RETRIEVAL_BUDGET_TOKENS_DOCS,
        token_budget_refs=RETRIEVAL_BUDGET_TOKENS_REFS,
        top_k_docs=k,
        top_k_refs=k,
        allowed_doc_ids=allowed,
    )


async def _semantic_layer(
    *, project_id: str, query: str, history: list | None,
    corpus: str, k: int,
    allowed: set[str] | None, warnings: list[str],
) -> list[dict]:
    """Layer B — semantic retrieval (embedding cosine) via retrieve_context.

    Post-filters the narrow (no body leak; memory exempt — provenance already
    ran inside retrieve_context). A layer failure appends
    `search_degraded:semantic` and yields [].
    """
    try:
        result = await _retrieve_semantic_result(
            project_id=project_id, query=query, history=history,
            corpus=corpus, k=k,
            allowed=allowed,
        )
        # WHY: retrieve_context returns graceful errors (not_configured /
        # embedding_failed) WITHOUT raising, so the except below never fires for them.
        # No-silent-degradation: an else here surfaces the broken semantic layer, else
        # the model reads 0 semantic hits as "no matches exist".
        if result.error:
            warnings.append(f"search_degraded:semantic:{result.error}")
            return []
        return _dedupe_semantic_hits(result.hits or [], allowed)
    except Exception as e:
        logger.warning("search_materials semantic layer failed: %s", e)
        warnings.append("search_degraded:semantic")
        return []


async def _assemble_search_result(
    *, user: dict, query: str, qlower: str, exact: bool,
    direct: list[dict], semantic: list[dict], k: int, warnings: list[str],
) -> dict:
    """Fuse the layers, access-filter, and assemble the model-facing result.

    # Fuse: semantic mode → reciprocal rank fusion (the two layers' scores are
    # incomparable — tier band vs cosine — so RRF uses only their order). Exact
    # mode → the direct list as ranked by rank_rows (no semantic layer ran).
    """
    fused = direct if exact else _rrf_merge(direct, semantic)
    accessible = await _filter_accessible(fused, user)
    merged = accessible[: max(k, await settings.get("AGENT_SEARCH_DIRECT_K"))]
    if exact and not merged and qlower:
        # No-silent-degradation (S3.2): an empty `exact` result must not read as
        # "nothing like this exists in the project". exact is a strict subset of
        # semantic, so an empty result means the literal string is absent — and
        # re-running it in other words cannot change that. Switching mode or
        # searching for a different thing can.
        warnings.append("exact_no_hits")
    result: dict = {"query": query, "hits": merged}
    if warnings:
        # Surface the degradation explicitly (no-silent-degradation), in the
        # RESULT the tool returns — the model reads it here and the chip renders
        # this same body verbatim, so the user reads it too.
        result["warnings"] = warnings
        # WHY `notice` and not `error`: `error` in a tool envelope means the CALL
        # FAILED (the contract is pinned on the chip's `outcome` field — see the
        # tool-result envelope handling in `driver/frames.py`), and a partial
        # search that returned hits is not a failed call.
        result["notice"] = (
            "search_materials partially failed (" + ", ".join(warnings) + "); "
            "hits may be incomplete — consider read_document directly."
        )
    return result


# ─── Two-layer search — the orchestrator's ARCH notes ─────────────────────────
#
# ARCH: `history` is threaded into retrieve_context so _rewrite_query (gated by
# CHAT_QUERY_REWRITE_ENABLED) can normalize the raw query using conversational
# turns. Default [] = raw query (back-compat).
#
# ARCH: search ranks WITHOUT an anchor — rank_rows is called with anchor_id=None,
# so ranking is tier → recency (the no-anchor branch). An anchor tier was
# evaluated and deliberately NOT wired: memory facts live under
# `.lore/system/memory` (maximally far from any user document) and usually match
# on content (tier 3), so anchoring on the open document would reorder tier 3
# away from memory and out of the top-k — silently demoting the project's
# distilled knowledge on every search. The `anchor_id` parameter on rank_rows
# stays as the recoverable primitive; do not delete it and do not wire it
# without a memory decision and a measurement.
#
# ARCH: when `scope_root` is set, BOTH layers are constrained to the subtree —
# the direct query gains an `id IN $allowed` filter and semantic hits outside
# the subtree are dropped before merge (post-filter; no body leak). An
# empty/None scope_root leaves whole-project behavior intact.
#
# ARCH(under_document_id): the model-facing subtree narrow. `allowed` is ALWAYS
# the intersection key_subtree ∩ requested_subtree — the key's scope is the
# ceiling, so a narrow can only narrow further, never widen. An out-of-scope or
# unknown root is an ERROR naming the remedy (never empty hits, which a model
# reads as "the project holds nothing"). Under a narrow, memory is filtered by
# PROVENANCE, not dropped: a fact belongs to the subtree iff any of its
# `mem.provenance.sources` resolves to a reference hosted there (the pushdown
# inside retrieve_context — see _allowed_fact_ids). Facts with empty/
# unresolvable sources drop (a measured legacy shortfall stated in the tool
# description), and the direct layer's `IN $allowed` keeps excluding fact-docs
# by tree position — under a narrow memory reaches the model via the semantic
# layer.
#
# ARCH(mode): `mode` routes the layers: `semantic` (default)
# runs the union predicate + the semantic layer + RRF; `exact` runs the direct
# layer with the CONTAINS disjunct alone and skips the semantic layer. (`intent`
# is display-only and never enters search — the activity layer reads it off the
# raw tool args; see reads.py.)
async def _search_materials_exec(
    *,
    project_id: str,
    user: dict,
    query: str,
    corpus: str = "all",
    k: int = 5,
    history: list | None = None,
    scope_root: str | None = None,
    under_document_id: str | None = None,
    mode: str = "semantic",
) -> dict:
    """Two-layer search(embedding lag): direct lexical (covers fresh docs whose
    embeddings lag) + semantic retrieval, fused via RRF, deduped by parent_id,
    per-doc access-filtered. The ARCH notes live above the def.
    """
    allowed = await _resolve_search_scope(
        project_id, scope_root, under_document_id,
    )
    # Per-layer degradation signals — each layer owns its catch and appends a
    # `search_degraded:<layer>` warning; the assembled result turns them into a
    # model-facing `error` note (never a silently empty hits array).
    warnings: list[str] = []
    qlower = (query or "").lower()
    exact = mode == "exact"

    db = await get_db()
    direct = await _direct_lexical_layer(
        db, project_id=project_id, query=query, qlower=qlower, exact=exact,
        allowed=allowed, corpus=corpus,
        k=k, warnings=warnings,
    )

    # `exact` skips the semantic layer — the CONTAINS scan alone, a matcher-level
    # subset of semantic (D9a). Top-k slices may differ; that is a slicing
    # artifact, not a recall surface.
    semantic: list[dict] = []
    if not exact:
        semantic = await _semantic_layer(
            project_id=project_id, query=query, history=history,
            corpus=corpus, k=k,
            allowed=allowed, warnings=warnings,
        )

    return await _assemble_search_result(
        user=user, query=query, qlower=qlower,
        exact=exact, direct=direct, semantic=semantic, k=k,
        warnings=warnings,
    )


async def _filter_accessible(hits: list[dict], user: dict) -> list[dict]:
    """Drop hits the user lacks per-doc access to.

    # WHY: a search snippet IS a read — gate it with the SAME per-doc check as
    # read_document (get_document_access != None), NOT mere project membership.
    # Why: a Viewer/Commentator with project access must not see a private doc's
    # snippet leak through the agent search layer.
    # Filter SEQUENTIALLY — never asyncio.gather: surrealdb-py multiplexes one WS
    # conn, so concurrent get_document_access calls contend (backend.md).
    """
    out: list[dict] = []
    for h in hits:
        if await get_document_access(h["doc_id"], user):
            out.append(h)
    return out


async def search_materials_tool(
    *,
    project_id: str,
    user: dict,
    query: str,
    corpus: str = "all",
    k: int = 5,
    history: list | None = None,
    scope_root: str | None = None,
    under_document_id: str | None = None,
    mode: str = "semantic",
) -> dict:
    """Public search_materials: two-layer search, per-doc access-filtered."""
    return await _search_materials_exec(
        project_id=project_id,
        user=user,
        query=query,
        corpus=corpus,
        k=k,
        history=history,
        scope_root=scope_root,
        under_document_id=under_document_id,
        mode=mode,
    )
