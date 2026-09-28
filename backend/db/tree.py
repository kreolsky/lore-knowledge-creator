"""Document tree traversal (ancestor / descendant walks).

Split out of the former backend/db.py (behavior-preserving).
"""

from __future__ import annotations

from db.pool import get_db
from db.records import extract_id


async def get_ancestor_ids(doc_id: str, project_id: str | None = None) -> list[str]:
    """Walk up the document parent chain, returning all IDs including doc_id.

    Stops at soft-deleted ancestors (they break the chain).

    # ARCH: walks parent_id by primary key — O(tree depth), NOT O(project size).
    # A full-project scan was ~10x slower on a 144-doc project (Fix 2, perf audit):
    # real trees are shallow (depth 2-4) while projects hold hundreds of docs, so
    # a few indexed PK lookups beat materializing every row. project_id is accepted
    # for API compatibility but no longer changes the strategy.
    """
    db = await get_db()

    # WHY: depth limit caps the sequential DB round-trips on pathologically
    # deep trees. Why: each level is one PK lookup; 50 bounds the worst case while
    # real trees (depth ≤ ~5) cost only a few ms.
    _MAX_ANCESTOR_DEPTH = 50
    ids: list[str] = []
    current: str | None = doc_id
    while current and len(ids) < _MAX_ANCESTOR_DEPTH:
        rows = await db.query(
            "SELECT parent_id, deleted_at FROM type::record('documents', $id)",
            {"id": current},
        )
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            break
        if row.get("deleted_at") is not None:
            break
        ids.append(current)
        parent_raw = row.get("parent_id")
        current = extract_id(parent_raw) if parent_raw else None
    return ids


async def get_descendant_ids(doc_id: str, project_id: str) -> list[str]:
    """Walk down the document tree, returning all descendant IDs (not including doc_id).

    Fetches all project docs in one query, builds children map in memory, BFS.
    Excludes soft-deleted documents.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT id, parent_id, deleted_at FROM documents WHERE project_id = $pid",
        {"pid": project_id},
    )
    children_map: dict[str, list[str]] = {}
    for row in (rows or []):
        rid = extract_id(row.get("id"))
        if not rid or row.get("deleted_at") is not None:
            continue
        parent = row.get("parent_id")
        if parent:
            pid = extract_id(parent) if not isinstance(parent, str) or ":" in parent else parent
            if pid:
                children_map.setdefault(pid, []).append(rid)

    # WHY: depth limit prevents unbounded traversal on deep trees.  Why: a cyclic parent_id (or a pathologically deep chain) would make the recursive walk loop; the hard cap bounds it.
    _MAX_DEPTH = 50
    result: list[str] = []
    queue = children_map.get(doc_id, [])[:]
    seen: set[str] = {doc_id}
    depth = 0
    while queue and depth < _MAX_DEPTH:
        next_queue: list[str] = []
        for cid in queue:
            if cid in seen:
                continue
            seen.add(cid)
            result.append(cid)
            next_queue.extend(children_map.get(cid, []))
        queue = next_queue
        depth += 1
    return result
