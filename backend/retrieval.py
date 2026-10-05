"""Retrieval pipeline — vector search, token budgeting."""
# ARCH: Per-kind vector search over doc_chunks (kind column, stamped at every
# chunk write) with anti-monopoly per-doc. References fold into the same corpus
# as documents — that folding fact has ONE wording source
# (models/references.py, module docstring) — and are labeled post-hoc as hits
# (kind="reference" vs "document" vs "memory") with separate token budgets per
# kind; the parent-join labels are a SECOND guard over the already-kind-scoped
# rows.
# ARCH: Dual-level retrieval (SYSTEM: memory). Project-memory fact-docs (is_memory=true)
# are a THIRD label on this one pipeline, not a second pipeline: their bodies are
# embedded by the existing pipeline, so they need no separate index. Two levels — FACT
# level (whole fact-docs, distilled and served with their source references) and CHUNK
# level (the raw documents and references) — with the fact level ranked ahead.
# SYSTEM: retrieval — RAG context retrieval for AI chat

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import settings

from config import count_tokens_approx
from db import get_db
from embeddings import EmbeddingConfigError, embed_texts
from models import is_ref_row

logger = logging.getLogger(__name__)




@dataclass
class RetrievalHit:
    kind: str
    parent_id: str
    parent_title: str
    heading: str | None
    snippet: str
    offset_start: int | None
    offset_end: int | None
    score: float
    # The references a fact was distilled from (`[{id, title}]`), read off
    # `mem.provenance.sources`. Only ever populated on kind="memory".
    # WHY(journal): a fact is served WITH its source references.
    # Why: the journal thread is what lets a reader check a distilled fact against
    # the material it came from; a fact whose origin is only in storage is unverifiable
    # at the one moment it is actually used.
    sources: list[dict] = field(default_factory=list)


@dataclass
class RetrievalResult:
    hits: list[RetrievalHit] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Limits:
    budget_docs: int
    budget_refs: int
    budget_mem: int
    top_k_docs: int
    top_k_refs: int
    top_k_mem: int


def _apply_drop_off(
    hits_list: list[RetrievalHit], drop_off: float,
) -> list[RetrievalHit]:
    # INVARIANT: hits_list must be sorted by score descending before calling.  Why: drop-off compares each hit's score to its predecessor's; without descending sort the predecessor is arbitrary and the relevance-cliff detection is meaningless.
    if len(hits_list) <= 1:
        return hits_list
    # WHY: Relative drop-off catches relevance cliff — a hit scoring 50% of the previous one
    # is likely off-topic, while absolute threshold misses topic transitions.
    cutoff = len(hits_list)
    for i in range(1, len(hits_list)):
        if hits_list[i].score < hits_list[i - 1].score * drop_off:
            cutoff = i
            break
    return hits_list[:cutoff]


async def _load_fact_payloads(db, mem_hits: list[RetrievalHit]) -> list[RetrievalHit]:
    """Give each fact hit its whole body and the references it was distilled from.

    Fetched in ONE extra query over the surviving fact ids only (never for every
    parent in the result set — an ordinary document's content can be arbitrarily
    large, and it is not what we return anyway). A fact-doc's `content` IS the fact
    (Order 2), so this is what makes the hit a fact rather than a fragment. A fact is
    one chunk, so there is nothing to collapse per parent.

    A body that comes back empty leaves the chunk snippet in place: a fact with no
    content is still better returned partially than dropped. A source whose reference
    row no longer resolves is dropped rather than titled with a guess.
    """
    if not mem_hits:
        return mem_hits
    ids = [h.parent_id for h in mem_hits]
    fact_rows = await db.query(
        "SELECT meta::id(id) AS id, content, mem FROM documents "
        "WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
        {"ids": ids},
    )
    bodies = {fr["id"]: (fr.get("content") or "") for fr in (fact_rows or [])}
    source_ids: dict[str, list[str]] = {
        fr["id"]: [
            str(s["id"]) for s in
            (((fr.get("mem") or {}).get("provenance") or {}).get("sources") or [])
            if s.get("kind") == "reference" and s.get("id")
        ]
        for fr in (fact_rows or [])
    }
    titles = await _reference_titles(db, {r for rs in source_ids.values() for r in rs})
    for h in mem_hits:
        body = bodies.get(h.parent_id)
        if body:
            h.snippet = body
        h.sources = [
            {"id": rid, "title": titles[rid]}
            for rid in source_ids.get(h.parent_id, [])
            if rid in titles
        ]
    return mem_hits


async def _reference_titles(db, ids: set[str]) -> dict[str, str]:
    """`{id: title}` for the reference rows that still exist — one query, empty on none."""
    if not ids:
        return {}
    rows = await db.query(
        "SELECT meta::id(id) AS id, title FROM documents "
        "WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
        {"ids": list(ids)},
    )
    return {r["id"]: (r.get("title") or "") for r in (rows or [])}


async def _allowed_fact_ids(db, project_id: str, allowed: set[str]) -> set[str]:
    """Ids of the live facts attributable to `allowed` by PROVENANCE.

    A fact belongs to the narrow iff ANY of its `mem.provenance.sources` (kind
    `reference`) resolves to a reference whose `parent_id` (the schema-enforced
    host, INVARIANT(reference-host)) is in `allowed` — a query-time join over
    the live reference rows, deliberately NOT a denormalized `hosts` copy on
    `mem.provenance` (references can move between hosts; a stamped copy drifts
    silently, the join is always honest).

    Two indexed queries: refs hosted in the subtree (idx_documents_parent_only),
    then the project's live fact rows whose `mem` is filtered here in Python.
    Soft-deleted references STILL resolve: provenance is a historical thread —
    only a hard-missing row counts as unresolvable, so the refs query carries
    no `deleted_at` filter. Facts with empty/unresolvable sources drop under a
    narrow (a measured legacy shortfall on dev — stated in the tool
    description, never silently).
    """
    ref_rows = await db.query(
        "SELECT VALUE meta::id(id) FROM documents "
        "WHERE project_id = $pid AND is_reference = true "
        "AND parent_id IN $allowed",
        {"pid": project_id, "allowed": list(allowed)},
    )
    hosted = set(ref_rows or [])
    if not hosted:
        return set()
    fact_rows = await db.query(
        "SELECT meta::id(id) AS id, mem FROM documents "
        "WHERE project_id = $pid AND is_memory = true AND deleted_at IS NONE",
        {"pid": project_id},
    )
    out: set[str] = set()
    for fr in fact_rows or []:
        sources = (((fr.get("mem") or {}).get("provenance") or {}).get("sources") or [])
        if any(
            isinstance(s, dict) and s.get("kind") == "reference" and s.get("id") in hosted
            for s in sources
        ):
            out.add(fr["id"])
    return out


async def _resolve_retrieval_limits(
    top_k_docs: int, top_k_refs: int, top_k_memory: int | None,
) -> _Limits:
    # WHY: docs/refs top_k always come from the caller (the agent's or the REST
    # client's `k`: 3 for a narrow lookup, 50 for broad exploration). Memory top_k
    # (None = the instance setting) and the budgets are instance settings.
    return _Limits(
        budget_docs=await settings.get("RETRIEVAL_BUDGET_TOKENS_DOCS"),
        budget_refs=await settings.get("RETRIEVAL_BUDGET_TOKENS_REFS"),
        budget_mem=await settings.get("RETRIEVAL_BUDGET_TOKENS_MEMORY"),
        top_k_docs=top_k_docs,
        top_k_refs=top_k_refs,
        top_k_mem=(top_k_memory if top_k_memory is not None
                   else await settings.get("RETRIEVAL_TOP_K_MEMORY")),
    )


async def _embed_user_query(
    user_query: str,
) -> tuple[list[list[float]] | None, str | None]:
    """Embed the query — `(vecs, None)`, or `(None, error)` on failure."""
    try:
        # Asymmetric embedding format — the query is prefixed with the
        # retrieval instruction; documents were embedded plain (no prefix). Opt-in here
        # only; embed_texts never prefixes by default (would poison stored vectors).
        query_embeddings = await embed_texts(
            [user_query],
            instruction=await settings.get("RETRIEVAL_QUERY_INSTRUCTION"),
        )
    except EmbeddingConfigError as e:
        logger.warning("Embedding config error: %s", e)
        return None, "not_configured"
    except Exception as e:
        logger.warning("Embedding API error: %s", e)
        return None, "embedding_failed"
    return query_embeddings, None


async def _narrow_scope(
    db, project_id: str, allowed_doc_ids: set[str], include_memory: bool,
) -> tuple[str, str, dict]:
    """The subtree narrow as a SQL pushdown, split PER KIND:
    `(doc_ref_clause, mem_clause, params)`.

    Doc/REF chunks scope by tree position — `document_id IN $allowed_docs`
    (a reference IS a documents row, so its own id sits in the allowed set).
    Fact chunks scope by PROVENANCE — `document_id IN $allowed_facts` from
    _allowed_fact_ids (an empty set excludes every fact when memory is off or
    nothing resolves). No overfetch constant: every row the fetch returns is
    already in scope.
    """
    allowed_facts: set[str] = (
        await _allowed_fact_ids(db, project_id, allowed_doc_ids)
        if include_memory else set()
    )
    return (
        "AND document_id IN $allowed_docs ",
        "AND document_id IN $allowed_facts ",
        {
            "allowed_docs": list(allowed_doc_ids),
            "allowed_facts": list(allowed_facts),
        },
    )


async def _kind_chunk_query(
    db, project_id: str, query_vec: list[float], *,
    kind: str, top_k: int, model: str, scope: str, scope_params: dict,
) -> list[dict]:
    """One kind's top-`top_k` chunks by cosine.

    # WHY (model = $model OR model IS NONE): NONE is "unknown, pre-column"
    # provenance, not "wrong" — excluding it would blank search on every
    # project for the ~48 h the stale-model sweep needs to drain post-deploy.
    # A row stamped with ANOTHER model is KNOWN-stale: its vector lives in
    # another model's space, its cosine against this query is noise.
    # WHY array::len(embedding) = $dim: vector::similarity::cosine RAISES on
    # mixed lengths — the guard turns a crash into an omission for the drain
    # window and for any future swap that changes the dimension (all vectors
    # are 1024-dim today; the guard is for the swap that changes that).
    # DEBT: archived refs remain in doc_chunks and are still citable by the agent
    # (the vector search does NOT exclude archived refs). If "archived = don't cite"
    # is ever wanted, add an `archived != true` guard here (joined on the parent
    # document).
    # Why deferred: archived means hidden from the LIST, not removed from the corpus;
    # whether it should also mean "don't cite" waits on the archive UX stabilizing.
    """
    rows = await db.query(
        "SELECT meta::id(id) AS id, document_id, heading, content, "
        "offset_start, offset_end, "
        "vector::similarity::cosine(embedding, $q) AS score "
        "FROM doc_chunks "
        "WHERE project_id = $pid AND kind = $kind "
        "AND (model = $model OR model IS NONE) "
        "AND array::len(embedding) = $dim "
        + scope
        + "ORDER BY score DESC LIMIT $top_k",
        {"q": query_vec, "pid": project_id, "kind": kind, "model": model,
         "dim": len(query_vec), "top_k": top_k, **scope_params},
    )
    return rows or []


async def _fetch_relevant_chunks(
    db, project_id: str, query_vec: list[float], *,
    include_documents: bool, include_references: bool, include_memory: bool,
    top_k_docs: int, top_k_refs: int, top_k_mem: int,
    allowed_doc_ids: set[str] | None,
) -> list[dict]:
    """One similarity query PER SWITCHED-ON KIND over doc_chunks (via
    `_kind_chunk_query`), narrowed in SQL when scoped — rows of the
    switched-on kinds are concatenated.

    # WHY per-kind queries (was: one project-wide summed LIMIT + post-label):
    # the post-LIMIT kind split starves an included kind under an excluded
    # majority — memory-only search on a document-heavy project returned 2
    # facts at top_k=8 (gray, F2): raw chunks consumed the LIMIT before any
    # fact was seen. Filtering `kind` BEFORE the LIMIT gives each included kind
    # its own full quota; `all` runs three scans of disjoint kinds (cosine
    # work unchanged, the row filter triples — measured before considering a
    # composite index).
    # WHY: `allowed_doc_ids` (None = whole project) is a subtree narrow pushed
    # into the SQL, not a post-filter. A post-filter fetches top-k project-wide
    # first, so out-of-scope chunks consume the top-k slots before the in-scope
    # ones are ever seen (slot starvation); the pushdown means every fetched row
    # is already in scope. Memory under a narrow is filtered by PROVENANCE,
    # never by tree position (see _allowed_fact_ids) — fact-docs live outside
    # every user subtree, so a tree-position filter drops ALL memory under any
    # narrow.
    """
    model = await settings.get("EMBEDDING_MODEL")
    scope_doc_ref, scope_mem = "", ""
    scope_params: dict = {}
    if allowed_doc_ids is not None:
        scope_doc_ref, scope_mem, scope_params = await _narrow_scope(
            db, project_id, allowed_doc_ids, include_memory,
        )
    rows: list[dict] = []
    for kind, top_k, scope in (
        ("document", top_k_docs if include_documents else 0, scope_doc_ref),
        ("reference", top_k_refs if include_references else 0, scope_doc_ref),
        ("memory", top_k_mem if include_memory else 0, scope_mem),
    ):
        if not top_k:
            continue
        rows.extend(await _kind_chunk_query(
            db, project_id, query_vec, kind=kind, top_k=top_k, model=model,
            scope=scope, scope_params=scope_params,
        ))
    return rows


async def _load_parent_meta(db, rows: list[dict]) -> dict[str, dict]:
    """`{document_id: {title, is_reference, is_memory}}` for the rows' parents.

    One query over the parents that actually surfaced. Parents that no longer
    resolve (soft-deleted between the chunk scan and here) are simply absent
    from the map, and the classifier drops their chunks.
    """
    if not rows:
        return {}
    parent_ids = list({r["document_id"] for r in rows})
    title_rows = await db.query(
        "SELECT meta::id(id) AS id, title, is_reference, is_memory "
        "FROM documents WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
        {"ids": parent_ids},
    )
    return {
        tr["id"]: {
            "title": tr.get("title", ""),
            "is_reference": is_ref_row(tr),
            "is_memory": bool(tr.get("is_memory")),
        }
        for tr in (title_rows or [])
    }


def _unpositioned_hit(kind: str, did: str, title: str, r: dict) -> RetrievalHit:
    """A memory/reference hit carries the chunk text but no heading/offsets —
    the position of the matching chunk inside a fact-doc or reference body is
    an implementation detail of the store, not something the model can use."""
    return RetrievalHit(
        kind=kind, parent_id=did, parent_title=title, heading=None,
        snippet=r["content"], offset_start=None, offset_end=None,
        score=r["score"],
    )


def _classify_chunk_hits(
    rows: list[dict], parent_meta: dict[str, dict], *,
    include_documents: bool, include_references: bool, include_memory: bool,
    min_score: float, max_per_doc: int,
) -> tuple[list[RetrievalHit], list[RetrievalHit], list[RetrievalHit]]:
    """Split chunk rows into `(doc, ref, mem)` hits by parent kind — the score
    floor, kind gates, one-per-fact and anti-monopoly cap all live here."""
    doc_hits: list[RetrievalHit] = []
    ref_hits: list[RetrievalHit] = []
    mem_hits: list[RetrievalHit] = []
    seen_facts: set[str] = set()
    doc_count: dict[str, int] = {}
    for r in rows:
        if r.get("score", 0) < min_score:
            continue
        did = r["document_id"]
        meta = parent_meta.get(did)
        if meta is None:
            continue
        if meta["is_memory"]:
            # INVARIANT: the fact level returns ONE hit per fact — the fact-doc,
            # not the chunk that matched.
            # Why: a fact-doc is one chunk, so the matched chunk IS the fact; the
            # whole body is substituted below, after the loop. (Order 2: a fact is a
            # document whose content IS the fact.)
            if not include_memory or did in seen_facts:
                continue
            seen_facts.add(did)
            mem_hits.append(_unpositioned_hit("memory", did, meta["title"], r))
            continue
        is_ref = meta["is_reference"]
        if is_ref and not include_references:
            continue
        if not is_ref and not include_documents:
            continue
        if is_ref:
            ref_hits.append(_unpositioned_hit("reference", did, meta["title"], r))
            continue
        # WHY: No more than max_per_doc chunks per document (anti-monopoly).  Why: without a per-doc cap one large document could fill the retrieval budget and crowd out every other doc; the cap keeps the context diverse.
        count = doc_count.get(did, 0)
        if count >= max_per_doc:
            continue
        doc_count[did] = count + 1
        doc_hits.append(RetrievalHit(
            kind="document", parent_id=did, parent_title=meta["title"],
            heading=r.get("heading"), snippet=r["content"],
            offset_start=r.get("offset_start"), offset_end=r.get("offset_end"),
            score=r["score"],
        ))
    return doc_hits, ref_hits, mem_hits


async def _classify_chunk_rows(
    db, rows: list[dict], *,
    include_documents: bool, include_references: bool, include_memory: bool,
) -> tuple[list[RetrievalHit], list[RetrievalHit], list[RetrievalHit]]:
    """Parent-meta join over `rows`, then pure classification (see
    `_classify_chunk_hits`)."""
    parent_meta = await _load_parent_meta(db, rows)
    return _classify_chunk_hits(
        rows, parent_meta,
        include_documents=include_documents,
        include_references=include_references,
        include_memory=include_memory,
        min_score=await settings.get("RETRIEVAL_MIN_SCORE"),
        max_per_doc=await settings.get("RETRIEVAL_MAX_PER_DOC"),
    )


async def _assemble_hits(
    db, doc_hits: list[RetrievalHit], ref_hits: list[RetrievalHit],
    mem_hits: list[RetrievalHit],
) -> list[RetrievalHit]:
    """Drop-off per kind, fact payloads, then the level ordering.

    # WHY: every fact-level hit precedes every chunk-level hit — a level
    # ordering, not a score bonus.
    # Why: a fact-doc is distilled and carries its sources where a chunk is raw, so it
    # outranks the material it came from even when that raw text is the closer
    # lexical match to the query. Sorting the three kinds together by score would make
    # this hold only by coincidence of cosine values, which is the state design P9 calls
    # "the structure buys nothing at query time".
    """
    drop_off = await settings.get("RETRIEVAL_SCORE_DROP_OFF")
    mem = _apply_drop_off(sorted(mem_hits, key=lambda h: h.score, reverse=True), drop_off)
    mem = await _load_fact_payloads(db, mem)
    doc = _apply_drop_off(sorted(doc_hits, key=lambda h: h.score, reverse=True), drop_off)
    ref = _apply_drop_off(sorted(ref_hits, key=lambda h: h.score, reverse=True), drop_off)
    return mem + sorted(doc + ref, key=lambda h: h.score, reverse=True)


def _apply_token_budgets(
    hits: list[RetrievalHit], *,
    budget_docs: int, budget_refs: int, budget_mem: int,
) -> list[RetrievalHit]:
    # WHY: Separate budgets per kind prevent document chunks from crowding out smaller reference hits.
    # INVARIANT: docs, refs and memory have separate token budgets — never mixed or
    # borrowed. Why: memory sharing the docs budget would let a wide document match
    # evict the distilled answer, which is the one hit that cost a consolidation run.
    budgets = {"memory": budget_mem, "document": budget_docs, "reference": budget_refs}
    spent = {"memory": 0, "document": 0, "reference": 0}
    budgeted: list[RetrievalHit] = []
    for h in hits:
        tokens = count_tokens_approx(h.snippet)
        if spent[h.kind] + tokens > budgets[h.kind]:
            continue
        spent[h.kind] += tokens
        budgeted.append(h)
    return budgeted


async def retrieve_context(
    project_id: str,
    user_query: str,
    *,
    include_documents: bool,
    include_references: bool,
    include_memory: bool = True,
    top_k_docs: int,
    top_k_refs: int,
    top_k_memory: int | None = None,
    allowed_doc_ids: set[str] | None = None,
) -> RetrievalResult:
    """Retrieve context for a query at two levels — fact (memory) and chunk.

    # ARCH: `include_memory` defaults to TRUE and is INDEPENDENT of
    # `include_documents`. Why: a fact-doc is `is_reference=false`, so folding it
    # into the documents flag would make "search the references only" also mean
    # "forget everything you know" — and no caller asking to narrow the raw corpus is
    # asking the agent to lose its memory. No persona or opt-in is required to read
    # memory; requiring one would make it a mode rather than memory.
    """
    if not (include_documents or include_references or include_memory):
        return RetrievalResult()
    limits = await _resolve_retrieval_limits(top_k_docs, top_k_refs, top_k_memory)
    query_vecs, error = await _embed_user_query(user_query)
    if error or not query_vecs:
        return RetrievalResult(error=error)

    db = await get_db()
    rows = await _fetch_relevant_chunks(
        db, project_id, query_vecs[0], include_documents=include_documents,
        include_references=include_references, include_memory=include_memory,
        top_k_docs=limits.top_k_docs, top_k_refs=limits.top_k_refs,
        top_k_mem=limits.top_k_mem, allowed_doc_ids=allowed_doc_ids,
    )
    doc_hits, ref_hits, mem_hits = await _classify_chunk_rows(
        db, rows, include_documents=include_documents,
        include_references=include_references, include_memory=include_memory,
    )
    hits = await _assemble_hits(db, doc_hits, ref_hits, mem_hits)
    return RetrievalResult(hits=_apply_token_budgets(
        hits, budget_docs=limits.budget_docs, budget_refs=limits.budget_refs,
        budget_mem=limits.budget_mem))
