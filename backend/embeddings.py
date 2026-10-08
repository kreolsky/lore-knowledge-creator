"""Embedding service — API client, incremental re-embed, debounce scheduler, event-bus
subscriber. The pure markdown chunker lives in markdown_chunker.py since the
extraction (importable with no DB/HTTP/event-bus)."""
# ARCH: Embeddings are computed lazily — EMBEDDING_API_URL validated on first use.
# ARCH: Trailing debounce via Redis deadline key + arq deferred job. Each content_flushed
#       sets embed:deadline:{id} = now + cooldown (TTL = cooldown*2) and enqueues
#       embed_document_task with defer=cooldown. Dedup prevents duplicate jobs.
#       When the deferred job runs it compares now vs the deadline; if a newer edit pushed
#       the deadline forward it re-defers itself via arq Retry (frees the worker slot),
#       otherwise it embeds. Net effect = re-embed cooldown after the LAST edit.
# SYSTEM: embeddings — vector embedding pipeline with chunking, API client, and debounce

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

import http_clients
import httpx
import settings
from agent_config_seed import is_help_doc_id
from markdown_chunker import Chunk, _breadcrumb_text, _chunk_hash, chunk_markdown

from config import EMBEDDING_CONCURRENCY
from db import get_db, run_in_transaction
from event_bus import emit as _bus_emit
from event_bus import on as _bus_on
from jobs import pool as jobs_pool
from models.references import is_ref_row

logger = logging.getLogger(__name__)


class EmbeddingConfigError(RuntimeError):
    pass


async def _ensure_config() -> str:
    """The resolved embedding API URL, raising EmbeddingConfigError when unset.

    Resolved per call (DB override → own env → AI_API_URL base) — a cached
    verdict would freeze "not configured" past an admin PUT that sets the URL,
    and the resolution itself is a dict lookup after the first load.
    """
    url = await settings.get("EMBEDDING_API_URL")
    if not url:
        raise EmbeddingConfigError("EMBEDDING_API_URL is not configured")
    return url


# Built once at import — the concurrency bound is restart-frozen by design
# (registry: EMBEDDING_CONCURRENCY, effect=restart).
_embed_semaphore = asyncio.Semaphore(EMBEDDING_CONCURRENCY)

# The embedding API client is the shared pool's "embeddings" entry
# (SYSTEM: http-clients) — reused across requests so the connection pool /
# keep-alive survives instead of being rebuilt per call. EMBEDDING_TIMEOUT_S
# rides each request as `timeout=`: the pool applies a build-time timeout
# only once, so a live override would otherwise never reach the client.
# Closed at shutdown by both the web lifespan (main.py) and the worker
# (_on_worker_shutdown), since embed_texts runs in both processes.


async def embed_texts(
    texts: list[str], *, instruction: str | None = None,
) -> list[list[float]]:
    """Embed `texts`. Pass `instruction` ONLY for a query (asymmetric embedding
    format, shared by giga/480m and Qwen3: `Instruct: …\\nQuery: …`); documents are embedded PLAIN.

    INVARIANT: `instruction` is opt-in per call site, never a default. A blanket prefix
    here would poison every stored document vector.  Why: the embedding model is asymmetric — documents embed PLAIN, only queries take the prefix; defaulting it here corrupts every stored vector. The four callers:
      _reembed (documents)            -> no instruction
      retrieval.py (the user query)   -> instruction=RETRIEVAL_QUERY_INSTRUCTION
      memory/_candidates.py (a body)  -> no instruction (it is a document, not a question)
      memory/dedup.py (fact vs fact)  -> no instruction (symmetric comparison)
    """
    if not texts:
        return []
    api_url = await _ensure_config()
    api_key = await settings.get("EMBEDDING_API_KEY")
    model = await settings.get("EMBEDDING_MODEL")
    batch_size = await settings.get("EMBEDDING_BATCH_SIZE")
    timeout_s = await settings.get("EMBEDDING_TIMEOUT_S")

    # Apply the query instruction per-input, only when explicitly requested.
    payload_inputs = (
        [f"Instruct: {instruction}\nQuery: {t}" for t in texts] if instruction else texts
    )
    results: list[list[float]] = []
    client = http_clients.get_http_client("embeddings", timeout=timeout_s)
    for i in range(0, len(payload_inputs), batch_size):
        batch = payload_inputs[i:i + batch_size]
        async with _embed_semaphore:
            resp = await client.post(
                f"{api_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "input": batch},
                timeout=timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()

        embeddings_sorted = sorted(data["data"], key=lambda d: d["index"])
        results.extend(d["embedding"] for d in embeddings_sorted)
    return results


async def _embed_texts_resilient(
    texts: list[str],
) -> tuple[list[list[float] | None], list[int]]:
    """Embed `texts`, isolating a per-string provider failure by bisection.

    Tries the full batch first (efficient). On an HTTP status error it bisects and
    retries each half, so a single unembeddable string is skipped (None) without
    discarding the rest of the document. Returns (results, failed_idx) where
    `results` is aligned to `texts` with None where the provider rejected the string.

    WHY: without this, one oversized/rejected string fails the whole batch and the
    ENTIRE document stays out of the index (ROOT CAUSE). Recursive splitting makes
    oversized strings impossible by construction; this is the residual defence for
    content the provider rejects for any other reason.
    """
    if not texts:
        return [], []
    try:
        embs = await embed_texts(texts)
        return embs, []
    except httpx.HTTPStatusError as e:
        # 429 (rate limit) / 5xx (provider down) are TRANSIENT and provider-wide —
        # bisecting only multiplies failing calls into a serialized O(N) storm (each
        # retry is its own POST). Propagate so the document fails fast and the debounce
        # retries the whole batch later. Only a 4xx content-rejection is per-string and
        # worth isolating by bisection.
        status = e.response.status_code if e.response is not None else 0
        if status == 429 or status >= 500:
            raise
        if len(texts) == 1:
            return [None], [0]
        mid = len(texts) // 2
        left_res, left_fail = await _embed_texts_resilient(texts[:mid])
        right_res, right_fail = await _embed_texts_resilient(texts[mid:])
        results = list(left_res) + list(right_res)
        failed = left_fail + [mid + i for i in right_fail]
        return results, failed


# ─── Incremental re-embed (D7/D11) ───────────────────────────────────────────
# A content_hash over the EMBEDDED text (breadcrumb included) lets a re-embed SKIP the
# embedding API call for chunks whose text did not change. The hash saves the EMBEDDING,
# not the DB write: a chunk whose text is unchanged but whose ord/offsets moved still
# gets an UPDATE in place (D7). A row with no hash (pre-S3) is never reused — the first
# re-embed after deploy re-embeds it (self-heal), then stores the hash for next time (D11).



def _plan_reuse(
    chunk_hashes: list[str], existing: list[dict],
) -> tuple[dict[int, str], list[int]]:
    """Match new chunk hashes against existing rows (multiset semantics).

    Returns (reuse, to_embed): `reuse` maps a new-chunk index to an existing row id whose
    hash matches (the row is claimed and will be UPDATEd in place, keeping its vector);
    `to_embed` lists the new-chunk indices that have no matching row and must be embedded.
    A row is claimed at most once, so two identical paragraphs do not cross-match one row.
    """
    available: dict[str, list[str]] = {}
    for r in existing:
        h = r.get("content_hash")
        if h:
            available.setdefault(h, []).append(r["id"])
    reuse: dict[int, str] = {}
    to_embed: list[int] = []
    for i, h in enumerate(chunk_hashes):
        bucket = available.get(h)
        if bucket:
            reuse[i] = bucket.pop()
        else:
            to_embed.append(i)
    return reuse, to_embed


def _unchanged(
    chunks: list[Chunk], chunk_hashes: list[str], existing: list[dict], model: str,
) -> bool:
    """True iff the new chunks' (hash, ord, offset_start, offset_end) match the
    existing rows exactly AND every row carries the CURRENT model stamp — a
    content_flushed that did not alter the document. Lets the re-embed skip the
    write entirely (no UPDATE, no version bump).

    WHY the model leg: a row whose provenance is unknown (pre-column, model IS
    NONE) or stale still OWES a `model` stamp even when its hash matches — the
    stale-model sweep (embedding_coverage) drains it through this very
    re-embed's reuse path ("reuses every row without a provider call and only
    writes model"). Skipping the write on a hash match alone would leave the
    document in the stale set forever: the sweep re-enqueues it, the hashes
    match again, nothing is written — a livelock that consumes a sweep slot
    every run.
    """
    if len(chunks) != len(existing):
        return False
    if any(r.get("model") != model for r in existing):
        return False
    new_sig = sorted(
        zip(chunk_hashes,
            (c.ord for c in chunks),
            (c.offset_start for c in chunks),
            (c.offset_end for c in chunks)))
    old_sig = sorted(
        (r.get("content_hash"), r.get("ord"), r.get("offset_start"), r.get("offset_end"))
        for r in existing)
    return new_sig == old_sig


# ─── Re-embed logic ──────────────────────────────────────────────────────────


# WHY: All embeddings live in doc_chunks keyed by document_id, so the legacy
# "ref" entity_type is treated the same as "doc" — the ref-folding fact has ONE
# wording source (models/references.py, module docstring).  Why: refs and docs share
# one chunk store and one retrieval path, so a second embedding path could only
# diverge from the corpus the retrieval reads. Any other entity_type is a no-op.
#
# ARCH (Order 2): a memory FACT is a document whose `content` IS the fact, so it
# chunks like any document (one fact = one chunk — the body is short authored
# prose, not a projection). A RETIRED fact is out of the index BY CONSTRUCTION:
# retirement soft-deletes it (the parent projection refuses to fetch it) and
# drops its vector (apply already deleted the chunks), so the debounce can
# never re-embed it back next to the fact that replaced it.


async def _fetch_reembed_parent(db, entity_id: str) -> dict | None:
    """The live parent row for a re-embed, or None (missing or soft-deleted).

    `is_memory`/`is_reference` feed the chunk's kind label (`_chunk_kind`); the
    legacy `mem` select was never read here and is gone."""
    parent = await db.query(
        # INVARIANT: the projection MUST filter `deleted_at IS NONE` — this clause
        # is what keeps a retired fact out of the re-embed path. Why: retirement
        # SOFT-DELETES (`_retire_fact` sets `deleted_at` in the same UPDATE), so a
        # retired fact returns no row here and the caller exits at `if parent is
        # None` — anything that re-embeds it would resurrect a ghost vector next
        # to the fact that replaced it.
        "SELECT content, content_version, is_memory, is_reference, title FROM documents "
        "WHERE meta::id(id) = $id AND deleted_at IS NONE",
        {"id": entity_id},
    )
    return parent[0] if parent else None


def _chunk_kind(parent: dict) -> str:
    """The corpus label stamped on every chunk of this parent — the SAME rule
    the direct-hit layer applies (agent/search_exec.py): memory if the parent
    is a fact, else reference, else document. `is_ref_row` keeps the strict
    boolean check (a half-typed legacy row must not read as a reference)."""
    if parent.get("is_memory"):
        return "memory"
    return "reference" if is_ref_row(parent) else "document"


async def _mark_embed_ok(db, entity_id: str, last_error: str | None = None) -> None:
    """Record a finished re-embed attempt: status 'ok', error cleared or stated."""
    if last_error is None:
        await db.query(
            "UPDATE type::record('documents', $id) SET "
            "embedding_status = 'ok', last_embed_error = NONE",
            {"id": entity_id},
        )
    else:
        await db.query(
            "UPDATE type::record('documents', $id) SET "
            "embedding_status = 'ok', last_embed_error = $err",
            {"id": entity_id, "err": last_error},
        )


async def _drop_all_chunks(db, entity_id: str) -> None:
    """Content became empty: nothing to embed — clear stale chunks, mark ok."""
    await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": entity_id})
    await _mark_embed_ok(db, entity_id)


async def _read_existing_chunks(db, entity_id: str) -> list[dict]:
    """Existing chunks: hash + position + model provenance, for reuse detection.
    `embedding` is intentionally NOT selected — a reused chunk is UPDATEd in
    place and KEEPS its stored vector, so the vector never crosses the wire
    (pulling every vector would be wasteful and, at corpus scale,
    memory-pressure the DB). Read BEFORE the transaction; the in-transaction
    content_version CAS still guards a concurrent embed writer. `model` feeds
    the `_unchanged` no-op gate: a row of unknown provenance is never
    "unchanged", so a pure-reuse re-embed still stamps it.
    """
    rows = await db.query(
        "SELECT meta::id(id) AS id, content_hash, ord, offset_start, offset_end, model "
        "FROM doc_chunks WHERE document_id = $id",
        {"id": entity_id},
    )
    return rows or []


async def _chunk_hashes(title: str, chunks: list[Chunk]) -> tuple[list[str], str]:
    """Content hashes for the chunks plus the model that produced them — ONE
    EMBEDDING_MODEL read per re-embed (plan: instance-settings-debt step 2),
    not one await per chunk. The caller threads the SAME value into
    doc_chunks.model (`_chunk_write_stmts`), never a second settings.get."""
    model = await settings.get("EMBEDDING_MODEL")
    return [_chunk_hash(title, c, model) for c in chunks], model


async def _reembed(entity_type: str, entity_id: str, project_id: str) -> None:
    if entity_type not in ("doc", "ref") or is_help_doc_id(entity_id):
        return
    try:
        await _ensure_config()
    except EmbeddingConfigError:
        logger.warning("Embedding not configured, skipping reembed for %s %s", entity_type, entity_id)
        return

    db = await get_db()
    parent = await _fetch_reembed_parent(db, entity_id)
    if parent is None:
        return
    content = parent.get("content") or ""
    version = parent.get("content_version", 0)
    title = parent.get("title") or ""

    chunks = chunk_markdown(
        content,
        max_chunk_chars=await settings.get("RETRIEVAL_CHUNK_MAX_CHARS"),
        input_max_chars=await settings.get("EMBEDDING_INPUT_MAX_CHARS"),
    )
    if not chunks:
        await _drop_all_chunks(db, entity_id)
        return

    kind = _chunk_kind(parent)
    store, failed_idx, model = await _reembed_store_plan(
        entity_type, entity_id, db, title, chunks,
    )
    if store is None:
        return  # no-op flush — already marked ok inside the plan
    stmts, params = _build_reembed_txn(
        entity_id, project_id, version, store, kind=kind, model=model,
    )
    try:
        await run_in_transaction(db, stmts, params)
    except RuntimeError as e:
        if "version_changed" in str(e):
            return  # CAS skip — a newer flush already re-embedded this doc.
        raise

    await _finalize_reembed(
        db, entity_type, entity_id, version, store=store, failed_idx=failed_idx,
    )


async def _reembed_store_plan(
    entity_type: str, entity_id: str, db, title: str, chunks: list[Chunk],
) -> tuple[list[tuple[Chunk, str, list[float] | None, str | None]] | None,
           list[int], str]:
    """Plan what this re-embed writes: `(store, failed_idx, model)`, or
    `(None, …, …)` when the flush changed nothing (no-op skip — status already
    marked 'ok' here).

    Hashes under the CURRENT model (D7: the hash mixes the model name, so a
    swap re-embeds everything), matches against the existing rows for reuse,
    short-circuits a byte-identical flush, and embeds the genuinely-changed
    chunks. The model rides the return so the caller stamps it onto every
    written row — never a second settings.get.
    """
    chunk_hashes, model = await _chunk_hashes(title, chunks)

    existing = await _read_existing_chunks(db, entity_id)
    reuse, to_embed_idx = _plan_reuse(chunk_hashes, existing)

    # No-op short-circuit: a content_flushed that did not alter the document. Nothing to
    # embed AND the structure (hash + ord + offsets) is byte-identical AND every row
    # carries the current model stamp — skip the write.
    if not to_embed_idx and _unchanged(chunks, chunk_hashes, existing, model):
        await _mark_embed_ok(db, entity_id)
        logger.info("Re-embed %s %s: no change (%d chunks reused)", entity_type, entity_id, len(chunks))
        return None, [], model

    store, failed_idx = await _embed_and_plan_store(
        entity_type, entity_id, title, chunks, chunk_hashes, reuse, to_embed_idx,
    )
    return store, failed_idx, model


async def reembed_now(entity_id: str, project_id: str) -> None:
    """Embed one document NOW — best-effort, never raises.

    The immediate counterpart of the debounced job, for callers that need the
    vector in the same request that wrote the content (the memory apply: the
    next portion's dedup gate and merge candidates query the vector, so a fact
    whose embedding arrives one cooldown later is invisible to them). A failure
    logs a WARNING and returns — the debounced job is still scheduled over the
    same content and catches up, so a provider blip costs the early vector,
    never the caller's response.
    """
    try:
        await _reembed("doc", entity_id, project_id)
    except Exception:
        logger.warning(
            "Inline reembed failed for %s; the debounced job will retry",
            entity_id, exc_info=True,
        )


async def _embed_and_plan_store(
    entity_type: str, entity_id: str, title: str,
    chunks: list[Chunk], chunk_hashes: list[str],
    reuse: dict[int, str], to_embed_idx: list[int],
) -> tuple[list[tuple[Chunk, str, list[float] | None, str | None]], list[int]]:
    """Embed only the genuinely-changed chunks; reused chunks keep their stored vector.

    Builds the store plan: each chunk is REUSED (UPDATE the matched row, keep its vector)
    or NEW (CREATE with a fresh embedding). A failed new chunk is dropped (partial
    success) — never silently sink the whole document (ROOT CAUSE §4). Returns
    `(store, failed_idx)`; store entries are `(chunk, hash, embedding, reuse_row_id)`.
    """
    if to_embed_idx:
        to_embed_texts = [_breadcrumb_text(title, chunks[i]) for i in to_embed_idx]
        results, failed_pos = await _embed_texts_resilient(to_embed_texts)
        fresh = {to_embed_idx[j]: results[j] for j in range(len(results))}
        failed_idx = [to_embed_idx[p] for p in failed_pos]
    else:
        fresh, failed_idx = {}, []
    store: list[tuple[Chunk, str, list[float] | None, str | None]] = []
    for i, c in enumerate(chunks):
        h = chunk_hashes[i]
        if i in reuse:
            store.append((c, h, None, reuse[i]))
        elif i in fresh and fresh[i] is not None:
            store.append((c, h, fresh[i], None))
    if not store:
        # Every changed chunk was rejected AND nothing was reusable — genuine failure.
        raise RuntimeError(
            f"embedding rejected all {len(to_embed_idx)} changed chunks for {entity_type} {entity_id}"
        )
    return store, failed_idx


def _chunk_write_stmts(
    store: list[tuple[Chunk, str, list[float] | None, str | None]], params: dict,
) -> list[str]:
    """Per-chunk UPDATE/CREATE statements; their params are appended onto `params`.

    ord is renumbered densely over the STORED chunks (a dropped middle chunk would
    otherwise leave a gap in the stored ord sequence). BOTH arms stamp
    `kind`/`model` ($kind/$model ride `params` from _build_reembed_txn) — a
    reused row is as much a corpus member as a fresh one, and a re-embed under a
    swapped model re-stamps its provenance without touching its vector.

    WHY: not db.record_refs — these are per-row UPDATE/CREATE statements (one
    type::record each), not an IN list; the ids already bind as params, which is
    the property record_refs exists to guarantee.
    """
    stmts: list[str] = []
    for i, (chunk, h, emb, rid) in enumerate(store):
        params.update({
            f"ord{i}": i, f"heading{i}": chunk.heading, f"content{i}": chunk.content,
            f"os{i}": chunk.offset_start, f"oe{i}": chunk.offset_end, f"hash{i}": h,
        })
        if rid is not None:
            # Reused: UPDATE the matched row in place — keep its stored vector, refresh
            # position + content_hash + kind/model + content_version.
            params[f"rid{i}"] = rid
            stmts.append(
                f"UPDATE type::record('doc_chunks', $rid{i}) SET "
                f"ord = $ord{i}, heading = $heading{i}, content = $content{i}, "
                f"offset_start = $os{i}, offset_end = $oe{i}, "
                f"content_hash = $hash{i}, kind = $kind, model = $model, "
                f"content_version = $ver"
            )
        else:
            # New/changed: CREATE with the fresh embedding.
            params.update({f"cid{i}": str(uuid4()), f"emb{i}": emb})
            stmts.append(
                f"CREATE type::record('doc_chunks', $cid{i}) SET "
                f"document_id = $id, project_id = $pid, ord = $ord{i}, "
                f"heading = $heading{i}, content = $content{i}, "
                f"offset_start = $os{i}, offset_end = $oe{i}, "
                f"content_hash = $hash{i}, kind = $kind, model = $model, "
                f"content_version = $ver, embedding = $emb{i}"
            )
    return stmts


def _build_reembed_txn(
    entity_id: str, project_id: str, version: int,
    store: list[tuple[Chunk, str, list[float] | None, str | None]],
    *, kind: str, model: str,
) -> tuple[list[str], dict]:
    """Atomic CAS + DELETE orphans + UPDATE reused + CREATE new. The content_version
    CAS is read INSIDE the transaction so a second embed writer of the same document
    can't slip its version bump between the reuse read above and the write — no flush
    bumps content_version (only the chunk arms and _finalize_reembed write it); the
    two writers are the inline fact embed and the debounced job. SurrealQL
    LET/IF/THROW does the CAS server-side; a THROW rolls back the whole transaction
    (the third statement class — UPDATE — is what D7 anticipated: the transaction
    grows, it does not simplify).

    `kind`/`model` ride the params into every chunk write (see
    _chunk_write_stmts) — one label + one provenance stamp per re-embed.
    """
    keep_ids = [rid for *_, rid in store if rid is not None]
    stmts = [
        "LET $cur = (SELECT VALUE content_version FROM documents WHERE meta::id(id) = $id)[0]",
        # INVARIANT(corruption): content_version must still match at write time, else another
        # embed writer already re-embedded newer content — skip rather than write stale chunks.  Why: two embed writers of one document overlap (the inline fact embed plus the debounced job) and the second can slip between the reuse-read above and this write; the server-side LET/IF/THROW CAS rejects it so stored chunks never regress to an older version. No flush bumps content_version — only the chunk arms and _finalize_reembed write it.
        "IF $cur != NONE AND $cur != $ver { THROW 'version_changed' }",
    ]
    params: dict = {
        "id": entity_id, "pid": project_id, "ver": version,
        "kind": kind, "model": model,
    }
    if keep_ids:
        params["keep_ids"] = keep_ids
        stmts.append("DELETE doc_chunks WHERE document_id = $id AND meta::id(id) NOT IN $keep_ids")
    else:
        stmts.append("DELETE doc_chunks WHERE document_id = $id")
    stmts.extend(_chunk_write_stmts(store, params))
    return stmts, params


async def _finalize_reembed(
    db, entity_type: str, entity_id: str, version: int, *,
    store: list[tuple[Chunk, str, list[float] | None, str | None]],
    failed_idx: list[int],
) -> None:
    """Version bump, then the embedding_status outcome and log line.

    # WHY: embedding_status reflects last reembed attempt outcome, independent
    # of CAS rejection.  Why: reused chunks always survive a partial embed, so the
    # document is genuinely indexed — 'error' would re-enqueue forever; the
    # partial-success policy instead marks 'ok' (stops re-enqueueing) and records
    # the dropped new-chunk count. Never silently degrade the index.
    """
    await db.query(
        "UPDATE type::record('documents', $id) SET content_version = $cv",
        {"id": entity_id, "cv": version + 1},
    )
    reused_n = len([rid for *_, rid in store if rid is not None])
    new_n = len(store) - reused_n
    if failed_idx:
        await _mark_embed_ok(
            db, entity_id,
            last_error=f"partial: {len(failed_idx)} new chunks rejected by provider",
        )
        logger.warning(
            "Re-embedded %s %s: %d reused, %d new, %d rejected",
            entity_type, entity_id, reused_n, new_n, len(failed_idx),
        )
    else:
        await _mark_embed_ok(db, entity_id)
        logger.info(
            "Re-embedded %s %s: %d chunks (%d reused, %d new)",
            entity_type, entity_id, len(store), reused_n, new_n,
        )


# ─── Debounce scheduler ──────────────────────────────────────────────────────


# WHY: per-project counter — embedding service typically fails project-wide
# (provider outage), not per-doc. Why: per-doc counters would spam toasts when
# 20 docs flush during an outage.
_consecutive_embed_failures: dict[str, int] = {}
_degraded_projects: set[str] = set()
EMBED_DEGRADED_THRESHOLD = 3


async def _on_content_flushed(entity_type: str, entity_id: str, project_id: str, **kwargs) -> None:
    # INVARIANT: the Lore guide is never embedded — not queued here, not a coverage
    # candidate (embedding_coverage.CANDIDATE_WHERE), not re-embedded (_reembed).
    # Why: operator ruling — the guide is reached through the agent prompt's pointer
    # and full-text search; embedding ~20 pages in every project, again on every
    # guide update, flooded the worker queue for hours and put Lore's docs into the
    # project's semantic search.
    if is_help_doc_id(entity_id):
        return
    skip_cooldown = False
    if entity_type in ("doc", "ref"):
        try:
            db = await get_db()
            status_row = await db.query(
                "SELECT embedding_status FROM documents WHERE meta::id(id) = $id AND deleted_at IS NONE",
                {"id": entity_id},
            )
            if status_row and status_row[0].get("embedding_status") == "failed":
                skip_cooldown = True
        except Exception:
            logger.debug("Could not read embedding_status for %s; using cooldown", entity_id)

    if skip_cooldown:
        await jobs_pool.enqueue("embed_document_task", entity_type, entity_id, project_id,
                      job_id=f"embed:{entity_id}")
    else:
        cooldown = await settings.get("EMBEDDING_COOLDOWN_SEC")
        deadline = time.time() + cooldown
        pool = await jobs_pool.get_arq_pool()
        await pool.set(f"embed:deadline:{entity_id}", str(deadline),
                       ex=cooldown * 2)
        await jobs_pool.enqueue("embed_document_task", entity_type, entity_id, project_id,
                      job_id=f"embed:{entity_id}", defer=cooldown)


async def _on_embed_failure(project_id: str) -> None:
    count = _consecutive_embed_failures.get(project_id, 0) + 1
    _consecutive_embed_failures[project_id] = count
    # INVARIANT: notify at exactly EMBED_DEGRADED_THRESHOLD consecutive failures
    # per project, sticky until recovery. Why: same rationale as save_degraded —
    # 1=noise, 2=transient, 3=clear signal. Sticky flag pairs with recovery event
    # so the toast clears itself when the provider comes back.
    if count >= EMBED_DEGRADED_THRESHOLD and project_id not in _degraded_projects:
        _degraded_projects.add(project_id)
        await _bus_emit("embedding_degraded", project_id=project_id)


async def _on_embed_success(project_id: str) -> None:
    _consecutive_embed_failures.pop(project_id, None)
    if project_id in _degraded_projects:
        _degraded_projects.discard(project_id)
        await _bus_emit("embedding_recovered", project_id=project_id)


_bus_on("content_flushed", _on_content_flushed)


# ─── Nearest-neighbour candidates(step 2) ───────────────


async def nearest_memory_facts(
    *, project_id: str, query_vec: list[float],
    exclude: tuple[str, ...] = (),
    limit: int | None = None,
) -> list[dict]:
    """Top-N memory entities whose stored chunks are nearest to `query_vec` (cosine).

    # WHY: best-effort enrichment, never a load-bearing field. Returns [] on any
    # miss (no entities, no chunks, a DB error) so a portion never fails because the
    # candidate surface could not be computed.
    # Why: the candidate list exists to stop a near-duplicate entity being created
    # next to an existing one; an empty list falls back to the flat `memory_index`,
    # which is the pre-candidate behaviour — degraded, not broken.
    """
    if limit is None:
        limit = await settings.get("MEMORY_MERGE_CANDIDATES")
    min_score = await settings.get("MEMORY_MERGE_CANDIDATE_MIN_SCORE")
    try:
        db = await get_db()
        ranked = await _rank_memory_chunks(db, project_id, query_vec, exclude, limit * 4)
    except Exception:
        logger.warning(
            "nearest_memory_facts: query failed; returning []", exc_info=True,
        )
        return []
    excluded = set(exclude)
    # Collapse to the BEST chunk per entity, drop the host + sub-floor noise, cap.
    best: dict[str, float] = {}
    for doc_id, score in ranked:
        if doc_id in excluded or score < min_score:
            continue
        if doc_id not in best or score > best[doc_id]:
            best[doc_id] = score
    top = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return await _hydrate_candidates(db, top)


async def _rank_memory_chunks(
    db, project_id: str, query_vec: list[float], exclude: tuple[str, ...], cap: int,
) -> list[tuple[str, float]]:
    """`(document_id, cosine)` per memory-entity chunk, highest first.

    The cosine lives in SurrealQL (`vector::similarity::cosine`); only the scalar
    scores cross the wire, never the vectors — the 1024-dim chunks stay in the DB.
    `cap` limits the rows transferred (ORDER BY sim DESC is computed over every
    chunk, no vector index yet, but only the top `cap` leave the DB).

    The model + dimension legs mirror retrieval's per-kind fetch exactly
    (`(model = $model OR model IS NONE)` — NONE is "unknown, pre-column"
    provenance, not wrong; `array::len(embedding) = $dim` because cosine RAISES
    on mixed lengths — a wrong-dimension row is omitted, never crashes the
    gate)."""
    excluded = set(exclude)
    mem_ids = [
        r["id"] for r in (await db.query(
            "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
            "AND is_memory = true AND deleted_at IS NONE", {"pid": project_id},
        ) or []) if r.get("id") and r["id"] not in excluded
    ]
    if not mem_ids:
        return []
    model = await settings.get("EMBEDDING_MODEL")
    return [
        (row["document_id"], row.get("sim") or 0.0)
        for row in (await db.query(
            "SELECT document_id, vector::similarity::cosine(embedding, $q) AS sim "
            "FROM doc_chunks WHERE document_id IN $ids "
            "AND (model = $model OR model IS NONE) "
            "AND array::len(embedding) = $dim "
            "ORDER BY sim DESC LIMIT $cap",
            {"ids": mem_ids, "q": query_vec, "model": model,
             "dim": len(query_vec), "cap": cap},
        ) or [])
    ]


async def _hydrate_candidates(
    db, top: list[tuple[str, float]],
) -> list[dict]:
    """Attach title to the ranked `(id, score)` pairs."""
    if not top:
        return []
    rows = {
        r["id"]: r for r in (await db.query(
            "SELECT meta::id(id) AS id, title FROM documents "
            "WHERE meta::id(id) IN $ids", {"ids": [i for i, _ in top]},
        ) or [])
    }
    return [
        {
            "id": doc_id,
            "title": (rows.get(doc_id) or {}).get("title") or "",
            "score": round(score, 4),
        }
        for doc_id, score in top
    ]
