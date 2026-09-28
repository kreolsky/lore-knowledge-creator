"""Document proximity + name-match ranking — backend mirror of document-sort.ts.

# SYSTEM: doc-proximity — tree-distance + name-tier ranking shared by the agent
# search layer. Contract mirrors frontend/src/utils/document-sort.ts; tests
# mirror document-sort.test.ts.

Ranking priority (identical to the parent-change UI): Name Match tier → Proximity
(tree steps from the anchor doc) → Recency (updated_at DESC) → sort_key ASC →
document_id ASC.

Tiers (name_match_tier):
  0 — title == query (case-insensitive)
  1 — title starts-with query
  2 — title contains query
  3 — only content contains query (title did not match)
  4 — no match (excluded by callers)

Distance (tree_distance): LCA walk over a project-wide parent map; the project
itself is a virtual root so disconnected subtrees still have a finite distance.
0 = same doc, 1 = parent/child, larger = farther.

The name→id resolution policy (resolve_doc_by_name & co., shipped in the same
plan as this module) was deleted: it had no callers, contradicted the live
"a title is not an address" contract on read_document, and skipped access
checks by design. `anchor_id`/`tree_distance` below stay as the ordering
primitive only.
"""
from __future__ import annotations

from datetime import datetime, timezone

# ARCH: project itself is a virtual root above all documents — disconnected
# subtrees still have a finite distance via the project node (mirrors
# document-sort.ts PROJECT_ROOT).
PROJECT_ROOT = "\0__project__"


def tree_distance(a: str | None, b: str | None, parent_map: dict) -> int:
    """Steps between a and b over `parent_map` ({id: parent_id|None}).

    Mirrors document-sort.ts.treeDistance. The project node (PROJECT_ROOT) sits
    above every root doc, so two disconnected subtrees have a finite distance.
    """
    if a == b:
        return 0
    if not a or not b:
        return 0

    depths_b: dict[str, int] = {}
    cur: str | None = b
    d = 0
    while cur:
        depths_b[cur] = d
        d += 1
        cur = parent_map.get(cur)
    depths_b[PROJECT_ROOT] = d

    cur = a
    d = 0
    while cur:
        hit = depths_b.get(cur)
        if hit is not None:
            return d + hit
        cur = parent_map.get(cur)
        d += 1
    return d + depths_b.get(PROJECT_ROOT, 0)


def name_match_tier(title: str | None, query: str, content: str | None = None) -> int:
    """Name-match tier (0..4). 3 = content-only match, 4 = no match."""
    t = (title or "").lower()
    q = (query or "").lower()
    if q and t == q:
        return 0
    if q and t.startswith(q):
        return 1
    if q and q in t:
        return 2
    if q and q in (content or "").lower():
        return 3
    return 4


def _ts(value) -> float:
    """Parse updated_at (datetime | iso str | None) → epoch seconds (0 if unset)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    else:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def rank_rows(
    rows: list[dict], *, anchor_id: str | None, query: str,
    parent_map: dict | None = None,
) -> list[dict]:
    """Stable-rank candidate rows by tier → distance → recency → sort_key → id.

    `rows` are pre-filtered candidates (title OR content substring match). The
    anchor distance tier is skipped when `anchor_id` is None (no-anchor fallback,
    matching sortDocumentsForChat's null-anchor branch). Each row is annotated
    with `_tier` so callers can map scores.

    `parent_map` SHOULD be the full project-wide map (all docs) so tree distance
    is computed over the real tree, not just the candidate subset. When omitted,
    a map is built from the candidate rows themselves (degraded proximity — only
    correct when the candidate set spans the whole tree).

    # WHY `anchor_id` stays although no caller passes it today: it is the
    # ordering primitive ("prefer what is near the open document"), tested with
    # a real anchor (test_doc_proximity.py). It is unwired ON PURPOSE — memory
    # facts live under `.lore/system/memory` (maximally far from any user
    # document) and usually match on content (tier 3), so anchoring would
    # reorder tier 3 away from memory and out of the top-k, silently demoting
    # the project's distilled knowledge on every search. Wiring it requires a
    # memory decision and a measurement; deleting it throws away the tested
    # path that decision would need.
    """
    for r in rows:
        r["_tier"] = name_match_tier(r.get("title"), query, r.get("content"))

    full_map = parent_map if parent_map is not None else {r.get("id"): r.get("parent_id") for r in rows}

    def sort_key(r: dict) -> tuple:
        tier = r["_tier"]
        dist = tree_distance(anchor_id, r.get("id"), full_map) if anchor_id else 0
        recency = -_ts(r.get("updated_at"))
        sort_key_v = str(r.get("sort_key") or "")
        doc_id = str(r.get("id") or "")
        return (tier, dist, recency, sort_key_v, doc_id)

    return sorted(rows, key=sort_key)
