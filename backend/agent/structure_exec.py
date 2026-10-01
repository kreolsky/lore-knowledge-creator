"""Agent read-only project-structure executor (auto-run inside the loop / Tool-API).

Part of the chat-agent-mode read-only tool surface. Extracted from
readonly_executors — internal helper module: the public wrapper
`get_project_structure_tool` is re-exported from there so every existing importer
keeps its path.

# ARCH: ONE rule on three axes — a ROOT call
# (no start_id) is ORIENTATION, an explicit start_id is ENUMERATION. Depth
# (default 2 vs whole subtree), references (hidden vs listed) and the
# system + Memory subtrees (door rows vs contents) all follow that one rule.
# Everything withheld is COUNTED on its parent row — the counters are the
# drill-down handle, so the map always names what it did not deliver.

# ARCH: the walk runs over ONE cheap topology query (id, parent_id, flags,
# sort_key per live document) held in memory; titles/media fields are selected
# for the VISIBLE ids only — topology rows never reach the model. child_count /
# n_references / subtree_total / subtree_depth are computed from the in-memory
# parent→children map, not per-node recursion.

# INVARIANT (security, IDOR): the ONE topology SELECT carries the project
# filter, and every id the walk emits comes from that map — a cross-project
# start_id matches no row and returns `documents: []` (no per-level re-check is
# needed because no second query can widen the set; the detail SELECT repeats
# the project filter anyway).
# Why: the pre-plan BFS enforced the filter per level for the same reason —
# the filter must bound EVERY row that reaches the model.

# ARCH (security): a scoped key's topology is CLIPPED to its scope_root BEFORE
# any counting. Why: counters computed over the whole project would leak the
# size and depth of the tree outside the wall — the scope_root override guards
# ids; this guards aggregates.
"""
from agent_config_seed import memory_folder_id
from share_guard import system_root_id

from db import extract_id, get_db
from models import is_ref_row

# ARCH (review): backstop cap on the VISIBLE rows a root call can emit. With the
# layered walk depth is the real bound and this almost never fires; it stays as
# the hard ceiling so a pathological project cannot blow the model's context.
# When hit the result carries `truncated: true`.
STRUCTURE_ROW_CAP: int = 1000

# The root call's default layering: project skeleton first, drill down with
# start_id / explicit depth. Root call = orientation.
ROOT_DEPTH_DEFAULT: int = 2

# BFS ceiling for the start_id path (whole-subtree default), mirroring the
# pre-plan `_bfs_subtree` cap.
SUBTREE_DEPTH_CAP: int = 10


def _parent_of(row: dict) -> str | None:
    parent = row.get("parent_id")
    return extract_id(parent) if parent else None


def _sk_norm(v) -> tuple:
    """Total-order normalization for mixed Surreal sort_key shapes."""
    if v is None:
        return (2, "")
    if isinstance(v, bool):
        return (1, str(v))
    if isinstance(v, (int, float)):
        return (0, float(v))
    return (1, str(v))


def _sibling_order(topo: dict, ids: list[str]) -> list[str]:
    """(is_index DESC, is_reference, sort_key ASC, id ASC) — the pre-plan
    SELECT's ORDER BY, now applied in memory. `is_reference` keeps refs in the
    band they occupied while keyless (NONE → trailing via _sk_norm): with keys,
    refs would interleave with docs by sort_key; the band pins the order the
    agent was shown."""
    def key(i: str):
        r = topo[i]
        return (
            not bool(r.get("is_index")),
            bool(r.get("is_reference")),
            _sk_norm(r.get("sort_key")),
            i,
        )
    return sorted(ids, key=key)


def _children_map(topo: dict) -> dict[str | None, list[str]]:
    """parent id → children ids (siblings ordered). A row whose parent is not
    in the map (deleted host / clipped wall) roots at the virtual root — the
    row is never silently dropped from a root call."""
    children: dict[str | None, list[str]] = {}
    for i, r in topo.items():
        p = _parent_of(r)
        key = p if p in topo else None
        children.setdefault(key, []).append(i)
    for key in children:
        children[key] = _sibling_order(topo, children[key])
    return children


def _subtree_ids(children: dict, root: str) -> set[str]:
    """root + all descendants (iterative DFS over the in-memory map)."""
    out = {root}
    stack = [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _subtree_stats(
    children: dict,
) -> tuple[dict[str, int], dict[str, int]]:
    """Per id: subtree SIZE (nodes incl. self) and HEIGHT (edges to deepest
    leaf). Iterative post-order — one pass, no recursion limits. A `visited`
    guard makes a malformed parent cycle terminate (stats are then simply
    partial for the cycle's members — the walk itself never enters it)."""
    size: dict[str, int] = {}
    height: dict[str, int] = {}
    visited: set[str] = set()
    for root_key in children:
        if root_key is None or root_key in size:
            continue
        stack = [(root_key, False)]
        while stack:
            node, done = stack.pop()
            if done:
                kids = children.get(node, [])
                size[node] = 1 + sum(size.get(c, 1) for c in kids)
                height[node] = 1 + max((height.get(c, -1) for c in kids), default=-1)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for c in children.get(node, []):
                if c not in visited:
                    stack.append((c, False))
    return size, height


def _levels(children: dict) -> dict[str, int]:
    """Graph level per id (virtual root's children = 1), through hidden nodes
    too — level is a property of the tree, not of visibility."""
    level: dict[str, int] = {}
    frontier = [(c, 1) for c in children.get(None, [])]
    while frontier:
        node, depth = frontier.pop()
        if node in level:
            continue
        level[node] = depth
        for c in children.get(node, []):
            if c not in level:
                frontier.append((c, depth + 1))
    return level


def _root_hidden_ids(
    topo: dict, children: dict, project_id: str,
) -> set[str]:
    """Root-call system exclusion, by ANCESTRY (share_guard's rule): everything
    below the deterministic system root is withheld EXCEPT the two door rows —
    the system root itself and the `Memory` folder (each stays a visible row
    carrying its counters). A flag filter would hide the folder and orphan its
    is_system=false children in the map."""
    sys_root = system_root_id(project_id)
    if sys_root not in topo:
        return set()
    doors = {sys_root, memory_folder_id(project_id)}
    return _subtree_ids(children, sys_root) - doors


def _root_visible(
    topo: dict, children: dict, *, max_depth: int,
    include_references: bool, hidden_ids: set[str],
) -> list[str]:
    """Layered walk from the virtual root, descending only through VISIBLE
    nodes (visibility is downward-closed: nothing below a withheld row is
    listed), capped at max_depth layers."""
    def listed(i: str) -> bool:
        if i in hidden_ids:
            return False
        return include_references or not is_ref_row(topo[i])

    visible: list[str] = []
    level = [c for c in children.get(None, []) if listed(c)]
    depth = 1
    while level and depth < max_depth:
        visible.extend(level)
        nxt: list[str] = []
        for p in level:
            nxt.extend(c for c in children.get(p, []) if listed(c))
        level = nxt
        depth += 1
    visible.extend(level)
    return visible


def _subtree_visible(
    topo: dict, children: dict, start_id: str, max_depth: int | None,
) -> list[str]:
    """BFS from start_id — the ENUMERATION path: every descendant (references
    and system contents included) up to max_depth, whole subtree when None."""
    visible = [start_id]
    seen = {start_id}
    frontier = [start_id]
    depth = 0
    cap = SUBTREE_DEPTH_CAP if max_depth is None else max(1, min(SUBTREE_DEPTH_CAP, max_depth))
    while frontier and depth < cap:
        nxt: list[str] = []
        for p in frontier:
            for c in children.get(p, []):
                if c not in seen:
                    seen.add(c)
                    nxt.append(c)
        visible.extend(nxt)
        frontier = nxt
        depth += 1
    return visible


def _counters(
    topo: dict, children: dict, visible: list[str],
    size: dict[str, int], height: dict[str, int], level: dict[str, int],
) -> dict[str, dict[str, int]]:
    """Per visible row: what this result did NOT deliver below it.

    child_count — withheld direct non-reference children; n_references —
    withheld direct reference children; subtree_total — withheld descendants;
    subtree_depth — max distance to a withheld descendant. Only non-zero
    counters are emitted (the row projection carries what is populated).
    """
    visible_set = set(visible)
    # visible strict-descendant count per visible row: each visible node adds 1
    # to every visible ancestor (climb terminates at the map's edge).
    visible_below: dict[str, int] = dict.fromkeys(visible, 0)
    for v in visible:
        p = _parent_of(topo[v])
        while p is not None and p in topo:
            if p in visible_set:
                visible_below[p] += 1
            p = _parent_of(topo[p])
    # Hidden-subtree depth per visible row, bottom-up over the visible set.
    hidden_depth: dict[str, int] = dict.fromkeys(visible, 0)
    for v in sorted(visible, key=lambda i: -level.get(i, 0)):
        best = 0
        for c in children.get(v, []):
            if c in visible_set:
                hd = hidden_depth.get(c, 0)
                if hd > 0:
                    best = max(best, 1 + hd)
            else:
                best = max(best, 1 + height.get(c, 0))
        hidden_depth[v] = best
    out: dict[str, dict[str, int]] = {}
    for v in visible:
        row: dict[str, int] = {}
        hidden_direct = [c for c in children.get(v, []) if c not in visible_set]
        n_ref = sum(1 for c in hidden_direct if is_ref_row(topo[c]))
        if len(hidden_direct) - n_ref:
            row["child_count"] = len(hidden_direct) - n_ref
        if n_ref:
            row["n_references"] = n_ref
        hidden_total = size.get(v, 1) - 1 - visible_below[v]
        if hidden_total > 0:
            row["subtree_total"] = hidden_total
        if hidden_depth[v] > 0:
            row["subtree_depth"] = hidden_depth[v]
        if row:
            out[v] = row
    return out


async def _load_topology(db, project_id: str) -> dict[str, dict]:
    """The ONE project-filtered topology query — flags + sort only; titles and
    media fields never leave this boundary's detail select."""
    rows = await db.query(
        "SELECT meta::id(id) AS id, parent_id, is_index, is_reference, "
        "is_system, is_memory, sort_key FROM documents "
        "WHERE project_id = $pid AND deleted_at IS NONE",
        {"pid": project_id},
    )
    if not isinstance(rows, list):
        return {}
    return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}


async def _load_details(
    db, project_id: str, ids: list[str], *, include_outline: bool = False,
) -> dict[str, dict]:
    """Title / media fields for the VISIBLE ids only (project filter repeated —
    see the IDOR INVARIANT on the module). With include_outline the two stored
    outline columns ride the same select — two more plain column reads, never
    a per-row content pull (ARCH: derived and STORED on the module)."""
    if not ids:
        return {}
    outline_cols = ", outline, outline_hidden" if include_outline else ""
    rows = await db.query(
        f"SELECT meta::id(id) AS id, title, media_type, source_url{outline_cols} "
        "FROM documents WHERE project_id = $pid AND deleted_at IS NONE "
        "AND meta::id(id) IN $ids",
        {"pid": project_id, "ids": ids},
    )
    if not isinstance(rows, list):
        return {}
    return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}


async def _get_project_structure_exec(
    *,
    project_id: str,
    start_id: str | None = None,
    depth: int | None = None,
    include_references: bool = False,
    include_outline: bool = False,
    scope_root: str | None = None,
) -> dict:
    """Flat project tree metadata (no content), project-scoped (no per-doc RBAC —
    metadata only, no content leak; the project membership check in get_agent_context
    / session gate is the boundary).

    routes.tool_api.tool_get_project_structure delegates here so both dispatch paths
    (HTTP endpoint for dsh + this in-editor loop executor) return the same shape.

    # ARCH: when `scope_root` is set it
    # OVERRIDES the caller's start_id — the agent cannot widen its view past the
    # wall, so the returned tree is the subtree only (root + descendants), and
    # the topology itself is clipped to the wall before any counting.
    """
    db = await get_db()
    # T5: a scoped key always enumerates from its subtree root, regardless of the
    # caller's start_id (the wall cannot be widened by request).
    if scope_root:
        start_id = scope_root
    # ARCH (enumeration-only): outlines ride the ENUMERATION mode, never the
    # root map — a root call can emit up to STRUCTURE_ROW_CAP rows and outlines
    # on all of them would destroy the layering the map exists for. The refusal
    # names the correction (server-decides rule 2). Checked AFTER the
    # scope_root override: the wall owns the seed, so a scoped key is already
    # on the enumeration path.
    if include_outline and not start_id:
        from fastapi import HTTPException

        # Ergonomics #4 override: the gateway's status-keyed 422 advice is the
        # full_rewrite one ("split into smaller edits") — wrong for this refusal,
        # whose fix is an argument. _err honors an exception-carried next_action.
        refusal = HTTPException(
            status_code=422,
            detail=(
                "include_outline requires start_id (enumeration only): pass the "
                "subtree root whose descendants to enumerate — a root "
                "orientation map cannot carry outlines."
            ),
        )
        refusal.next_action = "pass a start_id"
        raise refusal
    if depth is not None:
        try:
            depth = max(1, min(SUBTREE_DEPTH_CAP, int(depth)))
        except (TypeError, ValueError):
            depth = None
    topo = await _load_topology(db, project_id)
    children = _children_map(topo)
    if scope_root:
        # ARCH (security): clip BEFORE counting — counters computed over the
        # whole project would leak the size and depth of the tree outside the
        # wall. UNCONDITIONAL: a wall doc that is itself deleted/unknown clips
        # the map to nothing (empty result) — the wall owns the seed, so an
        # unknown wall id is an empty answer, never a widening one.
        allowed = _subtree_ids(children, scope_root)
        topo = {i: r for i, r in topo.items() if i in allowed}
        children = _children_map(topo)
    truncated = False
    if not start_id:
        visible = _root_visible(
            topo, children,
            max_depth=ROOT_DEPTH_DEFAULT if depth is None else depth,
            include_references=include_references,
            hidden_ids=_root_hidden_ids(topo, children, project_id),
        )
        # Backstop cut, so `truncated: true` never lies (the pre-plan SELECT's
        # LIMIT did the same): a pathological layering past the cap is cut at
        # the cap and flagged — counters are then computed on what was kept.
        if len(visible) > STRUCTURE_ROW_CAP:
            visible = visible[:STRUCTURE_ROW_CAP]
            truncated = True
    else:
        if start_id in topo:
            visible = _subtree_visible(topo, children, start_id, depth)
        else:
            # An unknown start_id returns the empty map (a miss is named by
            # the empty result + the caller's own re-listing, not by a guess).
            visible = []
    details = await _load_details(db, project_id, visible, include_outline=include_outline)
    size, height = _subtree_stats(children)
    level = _levels(children)
    counters = _counters(topo, children, visible, size, height, level)
    docs = []
    for i in visible:
        r = topo[i]
        d = details.get(i, {})
        row = {
            "document_id": i,
            "parent_id": _parent_of(r),
            "title": d.get("title") or "",
            "is_index": bool(r.get("is_index")),
            "is_reference": is_ref_row(r),
            "is_system": bool(r.get("is_system")),
            "is_memory": r.get("is_memory") is True,
        }
        if row["is_reference"]:
            # media_type + source_url so the reference-listing capability
            # (formerly list_references) stays derivable from this one call.
            # Emitted on REFERENCE rows only — every measured non-reference row
            # carried them empty.
            row["media_type"] = d.get("media_type") or ""
            row["source_url"] = d.get("source_url")
        elif include_outline:
            # Outline column:
            # enumeration rows can carry the stored heading outline so sibling
            # documents are tellable apart without opening them. Only for
            # non-reference rows with a NON-EMPTY outline; outline_hidden
            # follows the counters' projection rule — emitted only when the
            # degradation ladder actually dropped something. Without the flag
            # the row shape is byte-identical to before.
            if d.get("outline"):
                row["outline"] = d["outline"]
                if d.get("outline_hidden"):
                    row["outline_hidden"] = d["outline_hidden"]
        row.update(counters.get(i, {}))
        docs.append(row)
    result: dict = {"project_id": project_id, "documents": docs}
    if truncated:
        result["truncated"] = True
    return result


async def get_project_structure_tool(
    *,
    project_id: str,
    user: dict,
    start_id: str | None = None,
    depth: int | None = None,
    include_references: bool = False,
    include_outline: bool = False,
    scope_root: str | None = None,
) -> dict:
    """Public get_project_structure: flat tree metadata, project-scoped."""
    return await _get_project_structure_exec(
        project_id=project_id, start_id=start_id, depth=depth,
        include_references=include_references, include_outline=include_outline,
        scope_root=scope_root,
    )
