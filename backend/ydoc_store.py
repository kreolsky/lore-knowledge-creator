"""Y.Doc persistence — load, append updates, compact.

Manages the authoritative CRDT document state:
- load(entity_id) → pycrdt Doc (read ydoc_state + replay newer ydoc_updates)
- append_update(entity_id, update_bytes) → append to log
- compact(entity_id) → merge log → ydoc_state + derived content, prune log

# SYSTEM: ydoc-store — Y.Doc persistence with append-only update log and compaction
# ARCH: ydoc_state is the compacted snapshot; ydoc_updates is the append-only log.
#       On load, we read ydoc_state and replay any newer updates from the log.
#       maybe_compact merges log entries into ydoc_state + derived content, prunes the log.
#       Called after flush in session cleanup; threshold-gated so safe to call frequently.
# INVARIANT: content (documents.content) is a derived read-model — always computed
#            from the CRDT state, never written directly by collab code.  Why: content is projected from the CRDT on flush; a direct write would race the CRDT and diverge the read-model from the truth.
"""

from __future__ import annotations

import logging
import weakref

from backplane import YDOC_CHANNEL_PREFIX, get_backplane
from pycrdt import Doc, Text

from db import get_db, run_in_transaction
from table_serialize import capture_tables_json, expand_tables

logger = logging.getLogger(__name__)

COMPACT_MIN_UPDATES = 10

# INVARIANT(data-loss): surface, don't hide, durability degradation. Why: when append_update fails the
# in-memory Y.Doc has the edit and peers would converge on it via the backplane, but the
# append-only log does not — on restart the edit vanishes while peers believe they converged
# (silent data loss, violating the durability floor at session.py). We count failures and
# expose the total so /api/health / monitoring can alert. Mirrors event_bus backplane counters.
_durability_degraded_count = 0


def durability_degraded_count() -> int:
    """Total append-log durability failures since process start (for /api/health)."""
    return _durability_degraded_count

# INVARIANT(corruption): seeding a Y.Doc from plaintext content MUST use this fixed client_id,  Why: a random client_id per seed makes each seed a distinct CRDT client, so two seeds of the same text merge into duplicated content ('triples on re-enter'); the fixed id makes re-seeds idempotent.
# never a fresh random one. Two independent seeds of the same text under different
# client_ids are distinct CRDT items and MERGE into duplicated content (the
# "document text triples on re-enter" bug). A fixed id makes re-seeds idempotent.
# Why: chosen above 2**32 so it can never collide with a Yjs frontend client_id
# (Yjs picks a random uint32). See lessons/2026-05-30 + test_collab_persistence.
SEED_CLIENT_ID = 10_000_000_000


def seed_doc_from_content(content: str) -> Doc:
    """Build a Y.Doc from plaintext using the deterministic seed client_id."""
    doc = Doc(client_id=SEED_CLIENT_ID)
    text = doc.get("content", type=Text)
    if content:
        text += content
    return doc


def _doc_from_snapshot(snapshot: bytes | None) -> Doc:
    doc = Doc()
    if snapshot:
        doc.apply_update(snapshot)
    return doc


_text_cache: weakref.WeakKeyDictionary[Doc, Text] = weakref.WeakKeyDictionary()


def get_text(doc: Doc) -> Text:
    cached = _text_cache.get(doc)
    if cached is not None:
        return cached
    text = doc.get("content", type=Text)
    _text_cache[doc] = text
    return text


async def load(entity_id: str) -> Doc:
    """Load a Y.Doc from the persistent store.

    Reads ydoc_state (compacted snapshot) and replays any newer ydoc_updates.
    If no ydoc_state exists (pre-migration), builds from documents.content.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT ydoc_state, content FROM type::record('documents', $id)",
        {"id": entity_id},
        site="ydoc_load",
    )
    doc_row = rows[0] if rows else None
    snapshot = doc_row.get("ydoc_state") if doc_row else None

    if snapshot:
        doc = _doc_from_snapshot(snapshot)
    elif doc_row:
        doc = seed_doc_from_content(doc_row.get("content") or "")
    else:
        doc = seed_doc_from_content("")

    updates = await db.query(
        "SELECT payload, created_at FROM ydoc_updates "
        "WHERE document_id = $id ORDER BY created_at ASC",
        {"id": entity_id},
        site="ydoc_load",
    )
    for row in (updates or []):
        update_bytes = row["payload"]
        if update_bytes:
            doc.apply_update(update_bytes)

    return doc


async def append_update(entity_id: str, update_bytes: bytes) -> None:
    """Append a Yjs update to the append-only log."""
    db = await get_db()
    await db.query(
        "CREATE ydoc_updates CONTENT { document_id: $id, payload: $upd, created_at: time::now() }",
        {"id": entity_id, "upd": update_bytes},
        site="ydoc_append",
    )


async def publish_doc_update(entity_id: str, update_bytes: bytes, *, append: bool = True) -> None:
    """Fan a Y.Doc update out to live sessions on every replica (and optionally log it).

    # INVARIANT(corruption): every persisted content mutation must reach live sessions on ALL
    # replicas — publish to ydoc:{id} so subscribed sessions converge in real time.
    # Why: the worker (set_content) and REST (apply_external_content_change) paths used
    # to mutate content without publishing, so other replicas — and the worker's own
    # target editor — diverged until reload, and a live session could re-flush stale
    # in-memory content over the worker's DB write (silent data loss). Self-echo to the
    # publisher's own subscribed session is safe: applying an already-applied CRDT update
    # is a no-op (and Yjs dedupes on the client) — the guarantee handle_binary_message
    # already relied on.
    #
    # append=True: also append to the ydoc_updates log for replay durability (incremental
    # edits not yet in a snapshot — client sync, REST apply). append=False: the caller
    # already wrote an authoritative ydoc_state and pruned the log (set_content), so only
    # publish — appending would re-add a row to the just-cleared log.
    #
    # INVARIANT(corruption): the worker / REST replace-whole-document paths (set_content,
    # apply_external_content_change) publish a "delete-all + insert" update computed against
    # the snapshot they loaded.  Why: the deletes target items present in that loaded snapshot, so a concurrent peer's just-typed text (absent from it) is not covered and survives the merge — the known residual of replace-path concurrency. Under a CONCURRENT live editor on another replica, the CRDT
    # merge may NOT delete that peer's just-typed text (the delete ops don't cover items the
    # publisher never saw). This is inherent to "replace the whole document" against live
    # edits and is deliberately NOT fixed here — _write_lock + the rarity of the overlap
    # (transcription/agent/import vs. simultaneous manual typing on the same entity) keep it
    # acceptable. Why: the alternative (refusing the replace while anyone is editing) would
    # drop worker output; converging-with-possible-extra-text is the safer failure mode.
    """
    if append:
        try:
            await append_update(entity_id, update_bytes)
        except Exception:
            # INVARIANT(data-loss): on append failure do NOT publish to the backplane. Why: publishing a
            # non-durable edit makes peers converge on a state that vanishes on restart — a
            # strictly worse silent failure. Keeping the edit local-only turns data loss into a
            # DETECTED degradation (counter below + the local editor retries on the next append),
            # the lesser evil and the correct behavior per the project's no-silent-degradation
            # rule. This deliberately under-shoots the surrounding INVARIANT ("every persisted
            # mutation must reach live sessions") because the alternative is worse.
            global _durability_degraded_count
            _durability_degraded_count += 1
            logger.warning("Failed to append ydoc update for %s (durability degraded: %d)",
                           entity_id, _durability_degraded_count)
            return
    try:
        bp = get_backplane()
        await bp.publish(YDOC_CHANNEL_PREFIX + entity_id, update_bytes)
    except Exception:
        logger.warning("Backplane publish failed for %s", entity_id)


async def maybe_compact(entity_id: str) -> None:
    """Compact the update log if enough entries have accumulated.

    Merges all ydoc_updates into ydoc_state, derives plaintext content,
    and prunes the log. Safe to call frequently — exits early below threshold.

    # INVARIANT(data-loss): compaction durability — the prune deletes ONLY the exact row ids
    # that were replayed into the snapshot, never an unbounded `DELETE WHERE
    # document_id = $id`. Why: a concurrent append_update (another flush / backplane
    # / REST-apply, all calling publish_doc_update(append=True)) landing between
    # snapshot-capture and prune would be wiped by a document-scoped DELETE even
    # though it was never merged into ydoc_state — silent data loss on restart,
    # exactly what the append-only log exists to prevent. Deleting by the replayed
    # id set leaves any concurrently-appended row in the log (its id isn't in the
    # set) so it replays on the next load(). The UPDATE + DELETE run in ONE
    # transaction so a reader never observes a pruned log without its matching
    # ydoc_state (defense-in-depth for the read window). Why ids not a `created_at
    # <= cutoff`: under high write concurrency a concurrent append can share the
    # last replayed row's timestamp and a `<=` cutoff would wrongly delete it;
    # comparing by id is correct regardless of timestamp resolution. The composite
    # index idx_ydoc_updates_doc_created keeps the id-set DELETE cheap.
    """
    db = await get_db()

    rows = await db.query(
        "SELECT count() AS total FROM ydoc_updates WHERE document_id = $id GROUP ALL",
        {"id": entity_id},
        site="ydoc_compact",
    )
    total = rows[0].get("total", 0) if rows else 0
    if total < COMPACT_MIN_UPDATES:
        return

    # Capture the EXACT row ids that will be replayed into the snapshot, so the
    # prune below deletes only those rows (mirrors the query inside load()).
    # created_at is selected because SurrealDB requires the ORDER BY column to be
    # present in the projection (see auto_backup._latest_checkpoint_hash note).
    replayed = await db.query(
        "SELECT meta::id(id) AS rid, created_at FROM ydoc_updates "
        "WHERE document_id = $id ORDER BY created_at ASC",
        {"id": entity_id},
        site="ydoc_compact",
    )
    replayed_ids = [r["rid"] for r in (replayed or []) if r.get("rid")]

    doc = await load(entity_id)
    # Expand `![label](table:id)` anchors to GFM so persisted `documents.content`
    # (which feeds embeddings/search) carries the table text, not opaque anchors.
    content = expand_tables(doc)
    snapshot = doc.get_update()

    # Atomic: the snapshot write and the replayed-rows prune succeed or fail
    # together. Deleting by replayed id set (not document_id) is the actual fix;
    # the transaction is defense-in-depth for the read window.
    await run_in_transaction(
        db,
        [
            "UPDATE type::record('documents', $id) SET "
            "ydoc_state = $state, content = $content, updated_at = time::now()",
            "DELETE ydoc_updates WHERE document_id = $id AND meta::id(id) IN $replayed_ids",
        ],
        {"id": entity_id, "state": snapshot, "content": content,
         "replayed_ids": replayed_ids},
    )

    logger.debug("Compacted ydoc for document %s (%d updates merged)", entity_id, total)


async def derive_content(entity_id: str) -> str:
    """Compute the plaintext content from the CRDT state (table anchors → GFM)."""
    doc = await load(entity_id)
    return expand_tables(doc)


async def capture_live_state(document_id: str) -> tuple[str, str]:
    """Capture the raw anchor text + full tables JSON from the live Y.Doc.

    Single source for the live-state snapshot consumed by BOTH checkpoint writers
    that need a real restore point: ``create_checkpoint`` (content=None server-side
    create) and ``restore_checkpoint`` (the before-restore safety backup). Why: the
    two routes used to duplicate this capture line-for-line; the tables subtree is
    the most regression-prone surface in this subsystem (see the ARCH block on
    table_serialize), so a hand-maintained second copy would drift and silently make
    a manual create restore to a different state than a restore backup. Content and
    tables are read from the SAME loaded Y.Doc so anchors and table ids stay paired.

    Returns ``(raw_text, tables_json)`` where tables_json is always a string
    (``"{}"`` when the doc has no tables).

    Caller policy on a throw — pick by CONSEQUENCE, do not copy a neighbour at
    random. This never raises for "no live state" (``load`` seeds from
    documents.content when ydoc_state is absent, from ``""`` when the row is
    missing), so a throw means Surreal/CRDT breakage. A caller that would SERVE
    the fallback to a reader must refuse (``routes/public_share.py`` → 503:
    documents.content lags the live doc by every uncompacted update). A caller
    that only degrades an internal baseline may swallow it and log
    (``documents.update`` PATCH baseline); a caller with no meaningful
    fallback lets it propagate (``auto_backup.py``).
    """
    doc = await load(document_id)
    return str(get_text(doc)), capture_tables_json(doc)


async def set_content(entity_id: str, new_content: str, *, persist: bool = False, extra_sets: dict | None = None, tables_json: str | None = None) -> bytes:
    """Replace document content by mutating the Y.Doc (delete all + insert).

    Returns the resulting Yjs update bytes for broadcast.
    When persist=True, also writes ydoc_state + content to DB and prunes the
    update log — equivalent to a full compaction. Use this when no active
    collab session exists (REST apply paths).
    extra_sets: optional dict of additional SET fields merged into the UPDATE
                (e.g. {"processing_status": "ready"}).
    tables_json: when not None, rebuild the ``tables`` Yjs subtree from the captured
                JSON (see table_serialize.apply_tables_json) so a restore rebuilds the
                editable table blocks, not just the anchor text. None = legacy restore:
                leave the tables map untouched. Why None-not-"{}": a legacy checkpoint
                has no tables capture; clearing the map would silently drop live tables.

    # WHY: when tables_json is provided, apply_tables_json runs BEFORE
    # doc.get_update() so the broadcast snapshot carries the table rebuild (other
    # replicas converge in one frame).  Why: applying tables before doc.get_update() bakes the rebuild into the broadcast snapshot, so other replicas converge in one frame instead of a table-then-content flicker. The content Text and the tables map are mutated
    # against the SAME loaded Y.Doc in one logical replace.
    """
    from table_serialize import apply_tables_json

    doc = await load(entity_id)
    text = get_text(doc)
    old_len = len(text)
    if old_len > 0:
        del text[0:old_len]
    if new_content:
        text += new_content
    if tables_json is not None:
        apply_tables_json(doc, tables_json)
    snapshot = doc.get_update()
    if persist:
        db = await get_db()
        set_parts = "content = $c, ydoc_state = $state, updated_at = time::now()"
        params: dict = {"id": entity_id, "c": new_content, "state": snapshot}
        if extra_sets:
            for k, v in extra_sets.items():
                set_parts += f", {k} = ${k}"
                params[k] = v
        await db.query(
            f"UPDATE type::record('documents', $id) SET {set_parts}",
            params,
        )
        await db.query(
            "DELETE ydoc_updates WHERE document_id = $id",
            {"id": entity_id},
        )
        # Fan out to live sessions on every replica (append=False: ydoc_state above is
        # the authoritative snapshot and the log was just pruned). This is what lets a
        # worker reach the target editor without a paired apply_external_content_change.
        await publish_doc_update(entity_id, snapshot, append=False)
    return snapshot
