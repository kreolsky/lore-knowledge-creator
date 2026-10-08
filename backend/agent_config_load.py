"""Loader for the agent-config reserved subtree — BFS walk + bulk content fetch.

Split out of agent_config.py (now a façade). Resolves the skeleton folder ids,
builds a parent→children map via a per-level parent_id BFS (O(config) rows, not
O(project)), and fetches content only for the resolved config nodes. The assembled
dict feeds build_agent_system_prompt; load_skills_subtree is the scoped walk
behind the turn payload's raw skills wire (plan collapse-the-editor-harness-layer
step 3) so the prompt build and the payload share one config-tree walk.

# ARCH (perf): payload is proportional to the config subtree. Step 1: skeleton
# lookup keyed on is_system=true (idx_documents_system). Step 2: per-level
# parent_id BFS (idx_documents_sibling_order) from those known roots. Step 3:
# content fetched ONLY for the resolved config nodes.
"""
from __future__ import annotations

from agent_config_seed import _REQUIRED_ROLES, help_doc_id

from db import get_db, record_refs


def _walk_subtree(
    children_by_parent: dict[str, list[dict]], folder_id: str,
) -> list[dict]:
    """Pre-order DFS over a PREBUILT parent→children map (children already in
    sort_key order from the ORDER BY of the slim query). Returns every LIVE
    descendant row of `folder_id` at any depth. Children are plain docs (no
    is_system/role filter) — the tree is organization only; the whole live subtree
    is injected. The map is built once by the caller and reused for every folder."""
    result: list[dict] = []
    # DFS stack seeded in reverse so pop() yields first child first. `visited`
    # guards against a parent_id cycle (robustness — a cycle would otherwise loop
    # forever; no behavior change on valid tree data).
    visited: set[str] = {folder_id}
    stack = list(reversed(children_by_parent.get(folder_id, [])))
    while stack:
        node = stack.pop()
        if node["id"] in visited:
            continue
        visited.add(node["id"])
        result.append(node)
        for child in reversed(children_by_parent.get(node["id"], [])):
            stack.append(child)
    return result


async def _fetch_content(db, ids: list[str]) -> dict[str, str]:
    """Bulk-fetch content for a set of doc ids. Parametrized (no id interpolation)
    so doc ids never reach query text. Returns {id: content}."""
    if not ids:
        return {}
    refs, params = record_refs("documents", ids)
    rows = await db.query(
        f"SELECT meta::id(id) AS id, content FROM documents "
        f"WHERE id IN [{refs}] AND deleted_at IS NONE",
        params,
    )
    return {r["id"]: (r.get("content") or "") for r in (rows or [])}


def _fill_content(out: dict, content_map: dict[str, str]) -> None:
    """Stamp fetched content onto every placeholder node in `out` by id."""
    for v in out.values():
        nodes = v if isinstance(v, list) else [v] if isinstance(v, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = node.get("id")
            if nid and nid in content_map:
                node["content"] = content_map[nid]


async def _bfs_config_subtree(
    db, project_id: str, sp_folder_id: str | None, folder_ids: list[str],
) -> dict[str, list[dict]]:
    """Per-level BFS from the known config folder roots → parent→children map.

    # ARCH (perf): retrieves O(config) rows, not O(project). The subtree ROOTS are
    # deterministic ids, so one indexed query per depth level (parent_id IN $ids,
    # hitting idx_documents_sibling_order) replaces the old whole-project slim scan.
    # Config trees are 2–3 levels deep → 2–4 small round-trips.
    #
    # Personas are depth-1: the system_prompt folder seeds the frontier but its
    # children (personas) are NOT descended — grandchildren under a persona were
    # never personas and were never injected. The three cumulative
    # folders descend fully. `visited` guards against a parent_id cycle (robustness;
    # a cycle would also hang _walk_subtree — no behavior change on valid data).
    #
    # Per-level `ORDER BY sort_key` + grouping by parent yields the identical
    # per-parent child order the old global `ORDER BY sort_key` produced (Decision
    # 5): _walk_subtree's pre-order DFS over this map is unchanged.
    """
    children_by_parent: dict[str, list[dict]] = {}
    frontier = [fid for fid in ([sp_folder_id] + folder_ids) if fid]
    visited: set[str] = set(frontier)
    while frontier:
        # `is_reference DESC` keeps refs in the band they occupied before refs
        # got sort_keys (MEASURED: SurrealDB ORDER BY sort_key ASC places NONE
        # BEFORE strings, so a keyless ref led docs) — with keys, refs would
        # interleave with docs by sort_key; the band pins what the agent is told.
        rows = await db.query(
            "SELECT meta::id(id) AS id, system_role, title, sort_key, parent_id, "
            "is_reference FROM documents WHERE project_id = $pid AND parent_id IN $ids "
            "AND deleted_at IS NONE ORDER BY is_reference DESC, sort_key ASC",
            {"pid": project_id, "ids": frontier},
        )
        next_frontier: list[str] = []
        for r in (rows or []):
            children_by_parent.setdefault(r["parent_id"], []).append(r)
        for r in (rows or []):
            rid = r["id"]
            if rid in visited:
                continue  # cycle guard
            visited.add(rid)
            # Personas (children of the Personas folder) are terminal — depth-1.
            if r["parent_id"] != sp_folder_id:
                next_frontier.append(rid)
        frontier = next_frontier
    return children_by_parent


async def load_agent_system_docs(project_id: str) -> dict[str, object]:
    """Return system docs by role for prompt assembly.

    Skeleton singletons (`system_root`, `system_prompt`, `rules_folder`,
    `skills_folder`, `knowledge_folder`) are one dict each. `personas` is a list of
    the DIRECT children of the Personas folder (identified by parent_id, not a role
    tag); only the SELECTED one injects.

    For the three cumulative folders (rules/knowledge/skills) the WHOLE live subtree
    is loaded by walking parent_id to any depth (tree = organization only), returned
    under `rules_children` / `knowledge_children` / `skills_children` as a list of
    `{id,title,content}`. There is no is_system/role filter on descendants: children
    are plain docs.

    # ARCH (perf): three-phase load, payload proportional to the config subtree, NOT
    # the whole document tree. Step 1: a skeleton lookup keyed on is_system=true
    # (idx_documents_system) resolves the folder ids. Step 2: a per-level parent_id
    # BFS (idx_documents_sibling_order) from those known roots builds the
    # parent→children map (personas depth-1; folders full-depth). Step 3: content
    # is fetched ONLY for the resolved config nodes. Root/system_prompt stay empty
    # (never injected with content).
    """
    db = await get_db()

    # Skeleton lookup (indexed on is_system). Resolves the folder ids.
    skeleton = await db.query(
        "SELECT meta::id(id) AS id, system_role, title FROM documents "
        "WHERE project_id = $pid AND is_system = true AND deleted_at IS NONE",
        {"pid": project_id},
    )
    out: dict[str, object] = {}
    for r in (skeleton or []):
        role = r.get("system_role")
        if not role or role not in _REQUIRED_ROLES:
            continue
        out[role] = {"id": r["id"], "title": r.get("title") or "", "content": ""}

    sp_folder = out.get("system_prompt")
    sp_folder_id = sp_folder.get("id") if isinstance(sp_folder, dict) else None
    folder_ids: list[str] = []
    for folder_role in ("rules_folder", "knowledge_folder", "skills_folder"):
        folder = out.get(folder_role)
        fid = folder.get("id") if isinstance(folder, dict) else None
        if fid:
            folder_ids.append(fid)

    # BFS from the known roots → parent→children map (reused for personas
    # + every folder walk).
    children_by_parent = await _bfs_config_subtree(
        db, project_id, sp_folder_id, folder_ids,
    )

    # Personas = DIRECT children of the Personas folder (depth-1, positional).
    persona_ids: list[str] = []
    personas: list[dict] = []
    if sp_folder_id:
        for child in children_by_parent.get(sp_folder_id, []):
            persona_ids.append(child["id"])
            personas.append(
                {"id": child["id"], "title": child.get("title") or "", "content": ""}
            )
    out["personas"] = personas

    # Whole-subtree descendants for each cumulative folder (any depth, plain docs).
    relevant_ids: list[str] = []
    for folder_role, child_key in (
        ("rules_folder", "rules_children"),
        ("knowledge_folder", "knowledge_children"),
        ("skills_folder", "skills_children"),
    ):
        folder = out.get(folder_role)
        folder_id = folder.get("id") if isinstance(folder, dict) else None
        if not folder_id:
            out[child_key] = []
            continue
        relevant_ids.append(folder_id)
        descendants = _walk_subtree(children_by_parent, folder_id)
        out[child_key] = [
            {"id": d["id"], "title": d.get("title") or "", "content": "",
             # parent_id rides along for the folder-shape SKILL wire (heads vs
             # material — build_skill_docs groups on it); inert for
             # rules/knowledge, whose consumers read content flat.
             "parent_id": d.get("parent_id")}
            for d in descendants
        ]
        relevant_ids.extend(d["id"] for d in descendants)
    relevant_ids.extend(persona_ids)

    # Content only for the resolved config nodes.
    if relevant_ids:
        _fill_content(out, await _fetch_content(db, relevant_ids))
    return out


async def load_instance_skill_docs() -> list[dict]:
    """The wire's `instance` layer: every ENABLED instance_skills row WITH
    content, as raw documents (`location` is the stable marker
    "instance:<name>" — no project row exists to name). Instance-global, not
    project-scoped: the layer rides every project's turn payload and the
    plugin overlays it BETWEEN project and shipped (skills.ts
    resolveSkillCatalog). Rows with content NONE (pure tombstones over shipped
    skills) and disabled rows never serve here — a tombstone suppresses, it
    does not provide."""
    db = await get_db()
    rows = await db.query(
        "SELECT name, content FROM instance_skills "
        "WHERE enabled = true ORDER BY name"
    ) or []
    return [
        {"location": f"instance:{row['name']}", "content": row.get("content") or "",
         "child_docs": []}
        for row in rows
        if row.get("content")
    ]


async def load_instance_skill_tombstones() -> list[str]:
    """The wire's `instance_tombstones`: NAMES of disabled instance_skills
    rows — a separate NAME list (not raw bodies like the project tombstones)
    because the plugin derives project-tombstone names by PARSING bodies and
    an unparseable entry must suppress nothing; a disabled instance row has no
    body to parse, its name IS the key. Suppresses the shipped skill of that
    name exactly as a project tombstone does."""
    db = await get_db()
    rows = await db.query(
        "SELECT name FROM instance_skills WHERE enabled = false ORDER BY name"
    ) or []
    return [str(row["name"]) for row in rows]


async def load_suppressed_skill_contents(project_id: str) -> list[str]:
    """The RAW contents of the TOMBSTONED direct children of the Skills folder.

    Deleting a skill document is the off-switch for a shipped skill: the name
    the tombstone's frontmatter declares is suppressed, so the plugin's catalog
    does not serve the shipped file in its place (the plugin parses these raw
    contents — plan collapse-the-editor-harness-layer step 3; see
    resolveSkillCatalog's INVARIANT in harness-driver/plugin/src/skills.ts).

    Scoped to DIRECT children of the folder on purpose — a tombstoned node deeper
    in a folder-shaped skill is deleted MATERIAL, not a retired skill, and must
    not suppress anything. Copies retired by the one-time seeded-skills
    retirement are detached from the folder (parent_id cleared) and therefore
    never match.
    """
    db = await get_db()
    skel = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE project_id = $pid AND is_system = true "
        "AND system_role = 'skills_folder' AND deleted_at IS NONE",
        {"pid": project_id},
    )
    if not skel:
        return []
    rows = await db.query(
        "SELECT content, deleted_at FROM documents "
        "WHERE project_id = $pid AND parent_id = $fid",
        {"pid": project_id, "fid": skel[0]["id"]},
    ) or []
    return [
        row.get("content") or ""
        for row in rows
        # WHY: the tombstone test is Python truthiness, not a SurrealQL
        # `deleted_at IS NOT NONE` clause. Why: these columns hold NULL on some
        # rows and NONE on others, and the clause does not answer the same
        # question for both — a live drive had it count 66 rows as parented that
        # carried no parent at all. A missed tombstone here silently un-deletes
        # a skill the user turned off.
        if row.get("deleted_at")
    ]


async def load_skills_subtree(project_id: str) -> list[dict]:
    """Load ONLY the skills subtree (folder + descendants + content) — a scoped walk
    that skips rules/knowledge. Reuses the same per-level BFS + bulk content fetch as
    load_agent_system_docs. Feeds the turn payload's raw skills wire
    (build_prompt_and_skill_docs) so the prompt build and the payload share one
    config-tree walk."""
    db = await get_db()
    skel = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE project_id = $pid AND is_system = true "
        "AND system_role = 'skills_folder' AND deleted_at IS NONE",
        {"pid": project_id},
    )
    if not skel:
        return []
    folder_id = skel[0]["id"]
    children_by_parent = await _bfs_config_subtree(db, project_id, None, [folder_id])
    descendants = _walk_subtree(children_by_parent, folder_id)
    nodes = [{
        "id": d["id"], "title": d.get("title") or "", "content": "",
        # parent_id rides along for the folder-shape skill wire (heads vs
        # material — build_skill_docs groups on it).
        "parent_id": d.get("parent_id"),
    } for d in descendants]
    _fill_content(
        {"nodes": nodes},
        await _fetch_content(db, [d["id"] for d in descendants]),
    )
    return nodes


async def live_help_root(project_id: str) -> dict | None:
    """`{id, title}` of this project's Lore guide root, or None if deleted or moved out."""
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title FROM type::record('documents', $id) "
        "WHERE project_id = $pid AND deleted_at IS NONE",
        {"id": help_doc_id(project_id, "index"), "pid": project_id},
    )
    return rows[0] if rows else None
