"""Project routes — CRUD, search, document tree (members/invites in projects_members.py)."""
# SYSTEM: project-routes — project CRUD, search, members, document tree

import logging
from uuid import uuid4

from documents.service import last_doc_rule, live_doc_ids
from fastapi import APIRouter, Depends, HTTPException, Query
from model_access import PUBLIC_SUBJECT, own_group_subject, user_subjects
from project_members_service import group_member_ids
from share_guard import system_root_id
from sort_keys import key_between
from surrealdb import AsyncSurreal
from textmatch import anchor_pattern, fts_operand

from access import (
    get_project_access,
    is_instance_admin,
    is_project_root,
    max_access,
    require_admin_or_project_full,
    require_project_read,
)
from auth import get_current_user, require_user_manager_role
from db import (
    fetch_one,
    get_db,
    run_in_transaction,
    serialize_record,
    soft_delete,
    validate_record_id,
)
from event_bus import emit
from jobs import pool as jobs_pool
from models import (
    CreateProject,
    PatchProject,
    is_ref_row,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _enrich_projects(db: AsyncSurreal, projects: list[dict], user: dict, member_access: dict, last_doc_map: dict) -> list[dict]:
    """Add my_access, is_owner_like, last_accessed_doc_id, and owner_name to each project dict.

    `is_owner_like` is DERIVED from the already-fetched `member_access` map —
    owner, or an admin with a member row — never by calling is_project_root per
    project (that would add N no-LIMIT membership queries on the dashboard).
    `my_access` mirrors get_project_access truthfully: owner → full; else the
    MAX over member row (full for admins — elevated) and every audience row the
    caller's subjects match ('public' row → full for an admin); a private
    non-member project is NOT readonly (None).
    """
    uid = user["user_id"]
    is_admin = is_instance_admin(user)
    # The caller's grant subjects, read ONCE per list and only when some project
    # in the page carries an audience row (mirrors the lazy branch in
    # get_project_access — audience-less pages pay nothing).
    audience_subjects: set[str] | None = None
    if any(p.get("audiences") for p in projects):
        audience_subjects = set(await user_subjects(db, uid))
    for p in projects:
        pid = p["project_id"]
        if p.get("owner_id") == uid:
            p["my_access"] = "full"
        else:
            levels: list[str | None] = []
            if pid in member_access:
                levels.append("full" if is_admin else member_access[pid])
            if audience_subjects is not None:
                # mirror of get_project_access's audience loop: MAX over every
                # matching row; a 'public' row tops an admin up to full
                for row in p.get("audiences") or []:
                    subject = row.get("subject")
                    if subject in audience_subjects:
                        if subject == PUBLIC_SUBJECT and is_admin:
                            levels.append("full")
                        else:
                            levels.append(row.get("access_level"))
            p["my_access"] = max_access(*levels)
        # INVARIANT(security): the payload carries a capability flag, never the
        # instance role. Why: the frontend must not branch on `role` in TSX; a
        # future moderator role changes this predicate in ONE backend place.
        p["is_owner_like"] = p.get("owner_id") == uid or (is_admin and pid in member_access)
        if p.get("owner_id") == uid:
            # owner: keep projects.last_accessed_doc_id from SELECT * (set in documents.get_document)
            pass
        elif pid in last_doc_map:
            # last_doc_map already merged member rows + user_preferences fallback
            # (see _get_member_maps) — ordering matters: the merge happens before
            # this pop so public viewers resume their last doc.
            p["last_accessed_doc_id"] = last_doc_map[pid]
        else:
            # INVARIANT(security): non-members must not inherit the owner's last-accessed doc.  Why: last_accessed_doc_id is the owner's private nav state; leaking it to a non-member would expose which doc they last viewed, so it is stripped for non-members.
            # Why: SELECT * now carries projects.last_accessed_doc_id (owner's value);
            # a public-project viewer would otherwise be navigated to the owner's doc.
            p.pop("last_accessed_doc_id", None)

    # Liveness filter over every pointer this response would serve (owner column,
    # member row and user_preferences alike) — ONE batched query for the whole list.
    pointers = {p.get("last_accessed_doc_id") for p in projects}
    live = await live_doc_ids(db, {pid for pid in pointers if pid})
    for p in projects:
        # INVARIANT: a last-accessed pointer is served only while its document is live.
        # Why: deleting the doc you last opened left the pointer naming it, so entering
        # the project navigated into GET /documents/open/<id>, which 404s on a
        # soft-deleted row — the user was bounced back to the project list with
        # "Couldn't open this document" and could not enter the project at all.
        # Dropped here so the client's index_doc_id fallback wins.
        if p.get("last_accessed_doc_id") and p["last_accessed_doc_id"] not in live:
            p.pop("last_accessed_doc_id", None)

    owner_ids = list({p["owner_id"] for p in projects if p.get("owner_id")})
    if owner_ids:
        # SECURITY: validate IDs before f-string embedding in SurrealQL
        placeholders = [f"type::record('users', '{validate_record_id(oid)}')" for oid in owner_ids]
        owner_rows = await db.query(
            f"SELECT id, name FROM users WHERE id IN [{', '.join(placeholders)}]"
        )
        owner_map = {serialize_record(r, "user_id")["user_id"]: r["name"] for r in (owner_rows or [])}
    else:
        owner_map = {}
    for p in projects:
        p["owner_name"] = owner_map.get(p.get("owner_id"))

    # members_count for Dashboard chip. One batched GROUP BY query, then join in Python.
    project_ids = [p["project_id"] for p in projects]
    members_count: dict[str, int] = {}
    if project_ids:
        count_rows = await db.query(
            "SELECT project_id, count() AS c FROM project_members "
            "WHERE project_id IN $pids GROUP BY project_id",
            {"pids": project_ids},
        )
        members_count = {r["project_id"]: r["c"] for r in (count_rows or [])}
    for p in projects:
        p["members_count"] = members_count.get(p["project_id"], 0)

    return projects


async def _get_member_maps(db: AsyncSurreal, uid: str) -> tuple[dict[str, str], dict[str, str]]:
    """Fetch member access and last-doc maps for a user.

    # ARCH: last-doc uses the shared last_doc_rule (member row → user_preferences
    # → drop) — the SAME predicate as get_project's single-project read. Batched
    # over all of the user's projects so the dashboard is one round trip per table.
    """
    member_rows = await db.query(
        "SELECT project_id, access_level, last_accessed_doc_id FROM project_members WHERE user_id = $uid",
        {"uid": uid},
    )
    member_access = {r["project_id"]: r["access_level"] for r in (member_rows or [])}
    member_last: dict[str, str | None] = {
        r["project_id"]: r.get("last_accessed_doc_id") for r in (member_rows or [])
    }
    pref_rows = await db.query(
        "SELECT project_id, preferences FROM user_preferences WHERE user_id = $uid",
        {"uid": uid},
    )
    pref_last: dict[str, str | None] = {
        r["project_id"]: (r.get("preferences") or {}).get("last_accessed_doc_id")
        for r in (pref_rows or [])
    }
    last_doc_map: dict[str, str] = {}
    for pid in set(member_last) | set(pref_last):
        resolved = last_doc_rule(member_last.get(pid), pref_last.get(pid))
        if resolved:
            last_doc_map[pid] = resolved
    return member_access, last_doc_map


@router.get("/api/projects")
async def list_projects(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """List projects visible to the current user (owner + public + explicit membership + group audience)."""
    uid = user["user_id"]
    member_access, last_doc_map = await _get_member_maps(db, uid)
    # INCLUDES 'public' (every live user holds it) — the predicate carries the
    # public rows, so it is never empty for a live user.
    subjects = await user_subjects(db, uid)
    # INVARIANT(security): the audience predicate is CONDITIONAL — it joins the
    # query only when the caller holds at least one subject. Why: a
    # `audiences.subject CONTAINSANY $subjects` run over an EMPTY list (a
    # deleted user, user_subjects → []) must be omitted, not trusted to match
    # nothing — the operator semantics over an empty list are version-sensitive
    # and an `IN`-shaped degeneration leaked every private project once (the
    # pre-audiences trap). Same shape as the moderator scope_sql in
    # list_all_projects, for the same reason.
    audience_sql = " OR audiences.subject CONTAINSANY $subjects" if subjects else ""
    rows = await db.query(
        "SELECT * FROM projects WHERE deleted_at IS NONE AND "
        "(owner_id = $uid OR "
        "id IN (SELECT VALUE type::record('projects', project_id) FROM project_members WHERE user_id = $uid)"
        f"{audience_sql}) "
        "ORDER BY created_at DESC LIMIT $limit START $offset",
        {"uid": uid, "limit": limit, "offset": offset, **({"subjects": subjects} if subjects else {})},
    )

    projects = [serialize_record(r, "project_id") for r in (rows or [])]
    return await _enrich_projects(db, projects, user, member_access, last_doc_map)


@router.get("/api/admin/projects")
async def list_all_projects(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None),
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin: list ALL projects. Moderator: only projects owned by the own
    group — {me} ∪ active group members (the scoped admin panel)."""
    uid = user["user_id"]
    member_access, last_doc_map = await _get_member_maps(db, uid)

    # A moderator must not learn that a foreign project exists: the scope is a
    # WHERE clause, not a post-filter (a paged list would otherwise leak
    # foreign rows on pages the filter runs after).
    scope_sql = ""
    params: dict = {"limit": limit, "offset": offset}
    if user.get("role") == "moderator":
        uids_scope = [uid, *await group_member_ids(db, uid)]
        scope_sql = " AND owner_id IN $uids"
        params["uids"] = uids_scope

    # `?? ''` guards the nameless row: name is TYPE string, but a row created
    # without the field reads NONE, and string::lowercase(NONE) raises
    # InternalError — one such row would 500 the whole admin list (same guard
    # as the lexical layer's _contains_pred).
    # q also matches the OWNER's name (the card shows `| owner_name`, so an
    # admin filters "whose projects"): owner_id is a plain string, not a
    # record link, so the matching user ids are resolved first and joined in.
    name_filter = ""
    if q:
        params["q"] = q
        owner_rows = await db.query(
            "SELECT id FROM users WHERE string::lowercase(name ?? '') CONTAINS string::lowercase($q)",
            {"q": q},
        )
        params["owner_uids"] = [serialize_record(r, "user_id")["user_id"] for r in (owner_rows or [])]
        name_filter = (
            " AND (string::lowercase(name ?? '') CONTAINS string::lowercase($q)"
            " OR owner_id IN $owner_uids)"
        )
    rows = await db.query(
        "SELECT * FROM projects WHERE deleted_at IS NONE" + name_filter + scope_sql +
        " ORDER BY created_at DESC LIMIT $limit START $offset",
        params,
    )

    projects = [serialize_record(r, "project_id") for r in (rows or [])]
    return await _enrich_projects(db, projects, user, member_access, last_doc_map)


@router.post("/api/projects")
async def create_project(body: CreateProject, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Create a project with its index document.

    Both records are created in a single transaction to prevent partial state.
    The creator is the owner; owner access is implied by projects.owner_id and
    computed in access.get_project_access (returns 'full'). No project_members
    row is written for the owner.
    """
    project_id = str(uuid4())
    index_doc_id = str(uuid4())
    # INVARIANT: the index doc is a non-reference document, so it MUST get a sort_key at
    # creation (same invariant as documents.service.create_document).  Why: without a sort_key the doc is born sort_key=NONE and is filtered out of the tree query; key_between(None,None) seeds a valid fractional key (the lone top-level doc). A brand-new project
    # has no siblings yet, so the index doc is the lone top-level doc → key_between(None,
    # None). Why: without it the doc is born sort_key=NONE, which is filtered out of
    # the sibling list and breaks drag-reorder — there is no boot-time repair sweep.
    index_sort_key = key_between(None, None)

    # INVARIANT: owner is never a project_members row; Why: owner access is
    # implied by projects.owner_id and computed in access.get_project_access.
    # A row would only leak into GET /members with a meaningless role dropdown.
    await run_in_transaction(
        db,
        [
            "CREATE type::record('projects', $pid) CONTENT {"
            "  name: $name, description: $description, status: 'active', project_context: '',"
            "  index_doc_id: $idx_id, owner_id: $uid"
            "}",
            "CREATE type::record('documents', $idx_id) CONTENT {"
            "  project_id: $pid, parent_id: NONE,"
            "  title: 'project_context.md', content: '', path: 'project_context.md',"
            "  is_index: true, sort_key: $idx_sort_key"
            "}",
        ],
        {
            "pid": project_id, "idx_id": index_doc_id,
            "uid": user["user_id"], "name": body.name,
            "description": body.description,
            "idx_sort_key": index_sort_key,
        },
    )

    # WHY: the guide (see SYSTEM: help-subtree) is seeded by a worker job, not inline —
    # ~20 document writes would add seconds to every project create; the tree picks the
    # pages up from their creation broadcasts a moment later.
    await jobs_pool.enqueue("help_seed_task", project_id, job_id=f"help-seed:{project_id}")

    record = await fetch_one("projects", project_id)
    result = serialize_record(record, "project_id") if record else {
        "project_id": project_id,
        "name": body.name,
        "description": body.description,
        "index_doc_id": index_doc_id,
        "owner_id": user["user_id"],
        "audiences": [],
        "ref_image_preview": True,
    }
    result["my_access"] = "full"
    # The creator is the owner by construction (see the INVARIANT above).
    result["is_owner_like"] = True
    return result


def _subtree_descendants(children: dict[str, list[str]], root: str) -> set[str]:
    """BFS descendants of `root` over an in-memory children map (depth-capped).

    Mirrors db.get_descendant_ids's walk but operates on a prebuilt map, so it
    adds no DB round-trips. Depth-capped at 50 (same ceiling as the ancestor/
    descendant walks) to bound a cyclic parent_id.
    """
    out: set[str] = set()
    queue = children.get(root, [])[:]
    depth = 0
    while queue and depth < 50:
        nxt: list[str] = []
        for cid in queue:
            if cid in out:
                continue
            out.add(cid)
            nxt.extend(children.get(cid, []))
        queue = nxt
        depth += 1
    return out


def _public_share_coverage(
    documents: list[dict], share_rows: list[dict], project_id: str,
) -> set[str]:
    """Set of document_ids covered by a live anonymous public share.

    # WHY(coverage): mirrors the resolve funnel (public_share.py
    # `_find_share_row`). Why: the tree indicator must agree with the anonymous
    # read perimeter, or the owner sees an "orange" doc that 404s when opened
    # anonymously. So: doc-scope covers its own doc only; subtree-scope covers
    # the share root + all descendants; a doc-scope ancestor propagates nothing;
    # and the agent-config subtree is excluded even with a pre-guard row (the
    # resolve path 404s it by ancestry).
    #
    # WHY(perf): pure + in-process — builds the children map from the
    # already-loaded documents, adding ZERO DB round-trips regardless of tree
    # size. Why: a per-doc resolve_share call would be N round-trips on every
    # tree load (plan "orange-tree-icon" Order 1).
    """
    # WHY(coverage): only keep edges between ALIVE docs — get_ancestor_ids
    # BREAKS at a soft-deleted ancestor. Why: a subtree share on a deleted root
    # must not paint its orphaned children (doc deletion does not cascade to
    # children — cascade.py::_cascade_delete_document).
    alive = {d["document_id"] for d in documents}
    children: dict[str, list[str]] = {}
    for d in documents:
        parent = d.get("parent_id")
        if parent and parent in alive:
            children.setdefault(parent, []).append(d["document_id"])

    covered: set[str] = set()
    for r in share_rows:
        root = r.get("document_id")
        scope = r.get("scope")
        if not root or scope not in ("doc", "subtree"):
            continue
        covered.add(root)
        if scope == "subtree":
            covered |= _subtree_descendants(children, root)
    # Exclude the agent-config subtree (deterministic system root + descendants).
    sys_root = system_root_id(project_id)
    covered.discard(sys_root)
    covered -= _subtree_descendants(children, sys_root)
    return covered


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Return project with its document tree, members, and caller's access level."""
    await require_project_read(project_id, user)
    record = await fetch_one("projects", project_id)
    if not record:
        raise HTTPException(status_code=404, detail="Project not found")

    # WHY: tree view excludes reference-documents. References live in the
    # right-panel ReferencesPanel, not in the navigable document tree.  Why: references are a separate object class shown in ReferencesPanel; listing them in the tree would double them and break the drag/reorder semantics that assume real documents.
    # ORDER BY sort_key (fractional sibling order); document_id tie-breaks equal keys
    # from concurrent drags. is_index DESC keeps the index doc first.
    #
    # WHY (plan "unify-agent-config" / "persona-by-folder-position"): system
    # docs (is_system=true) are intentionally NOT filtered out here — they stay
    # VISIBLE in the tree. Why: the persona picker (ChatInput) selects personas by
    # parent_id — the direct children of the Personas folder (the doc with
    # system_role === 'system_prompt') — so the folder must be present in this
    # payload, and its plain-doc children are already returned too. Hiding system
    # docs would break that. The agent-brain subtree renders like ordinary docs.
    doc_rows = await db.query(
        "SELECT id, project_id, parent_id, title, path, is_index, sort_key, "
        "is_system, system_role, created_at, updated_at "
        "FROM documents WHERE project_id = $pid AND deleted_at IS NONE "
        "AND is_reference = false ORDER BY is_index DESC, sort_key ASC, id ASC",
        {"pid": project_id},
    )
    documents = [serialize_record(r, "document_id") for r in (doc_rows or [])]

    # Doc-tree key indicator (plan "glimmering-knitting-pebble"): annotate each
    # doc with the CALLER'S own non-internal key capabilities. One batched SELECT
    # (never per-doc); internal (agent-minted) keys and other users' keys never surface.
    key_rows = await db.query(
        "SELECT document_id, capabilities FROM api_keys "
        "WHERE user_id = $uid AND project_id = $pid AND internal = false "
        "AND deleted_at IS NONE",
        {"uid": user["user_id"], "pid": project_id},
    )
    caps_by_doc: dict[str, set] = {}
    for r in (key_rows or []):
        did = r.get("document_id")
        if did:
            caps_by_doc.setdefault(did, set()).update(r.get("capabilities") or [])
    for d in documents:
        caps = caps_by_doc.get(d["document_id"])
        if caps:
            d["key_capabilities"] = sorted(caps)

    # Doc-tree public-share indicator (plan "orange-tree-icon"): a document
    # covered by a live anonymous public link (document_shares) paints its tree
    # icon ORANGE — owner-only.
    # INVARIANT(security): emitted only to projects.owner_id == caller. Why:
    # mint/revoke is owner-gated (INVARIANT(security) in routes/document_shares.py),
    # so a non-owner cannot act on the information and keeps today's payload
    # unchanged. Precedence vs the key indicator (red/blue) is resolved in the
    # frontend (utils/key-icon.ts): agent > public > widget.
    if record.get("owner_id") == user["user_id"]:
        share_rows = await db.query(
            "SELECT document_id, scope FROM document_shares "
            "WHERE project_id = $pid AND deleted_at IS NONE",
            {"pid": project_id},
        )
        if share_rows:
            public_ids = _public_share_coverage(documents, share_rows, project_id)
            for d in documents:
                if d["document_id"] in public_ids:
                    d["public_share"] = True

    project_data = serialize_record(record, "project_id")
    project_data["my_access"] = await get_project_access(project_id, user)
    project_data["is_owner_like"] = await is_project_root(record, user)

    user_id = user["user_id"]
    if record.get("owner_id") == user_id:
        # owner: project_data already carries projects.last_accessed_doc_id from the SELECT.
        pass
    else:
        member_rows = await db.query(
            "SELECT last_accessed_doc_id FROM project_members "
            "WHERE project_id = $pid AND user_id = $uid",
            {"pid": project_id, "uid": user_id},
        )
        member_last_doc = member_rows[0].get("last_accessed_doc_id") if member_rows else None
        # Fallback: public-project viewers keep last-doc in user_preferences
        # (no membership row). Uses the shared last_doc_rule so this single-project
        # path and the batched _get_member_maps cannot disagree.
        if not member_last_doc:
            from documents.service import get_prefs_last_doc
            member_last_doc = last_doc_rule(member_last_doc, await get_prefs_last_doc(db, user_id, project_id))
        if member_last_doc:
            project_data["last_accessed_doc_id"] = member_last_doc
        else:
            # INVARIANT(security): non-members must not inherit the owner's last-accessed doc.  Why: last_accessed_doc_id is the owner's private nav state; leaking it to a non-member would expose which doc they last viewed, so it is stripped for non-members.
            project_data.pop("last_accessed_doc_id", None)

    # Same liveness gate as the list path (see the INVARIANT in _enrich_projects) —
    # and this is the endpoint the Dashboard reads immediately before navigating,
    # so a pointer left here lands the user on a 404 open.
    pointer = project_data.get("last_accessed_doc_id")
    if pointer and pointer not in await live_doc_ids(db, {pointer}):
        project_data.pop("last_accessed_doc_id", None)

    return {
        "project": project_data,
        "documents": documents,
    }


_SEARCH_PROJECTION = (
    "SELECT id, title, content, is_reference, parent_id, media_type, created_at, updated_at "
    "FROM documents "
)


async def _contains_scan(
    db: AsyncSurreal, project_id: str, q_lower: str,
    scope_clause: str = "", scope_params: dict | None = None,
) -> list:
    """Case-insensitive literal-substring scan — the exact mode, the empty-FTS-operand
    mode, and the FTS-failure fallback share it."""
    return await db.query(
        _SEARCH_PROJECTION +
        "WHERE project_id = $pid AND deleted_at IS NONE "
        + scope_clause +
        "AND ("
        "  string::contains(string::lowercase(title), $q) "
        "  OR string::contains(string::lowercase(content ?? ''), $q)"
        ") "
        "ORDER BY updated_at DESC LIMIT 50",
        {"pid": project_id, "q": q_lower, **(scope_params or {})},
    )


@router.get("/api/projects/{project_id}/search")
async def search_documents(
    project_id: str,
    q: str = Query(..., min_length=3),
    exact: bool = Query(False),
    under_document_id: str | None = Query(None),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Full-text search across project documents and references.

    Uses the FULLTEXT indexes (idx_documents_fts_title/_content) for the scan; ranking
    stays Python-side match_count (see below). The @0@/@1@ numbered match operators pair
    each field to its index — the required form for a cross-index OR.

    WHY the operand preprocessing: SurrealDB FULLTEXT matches ANY token (OR), so the
    RAW query fan-outs to every doc sharing a stop word or a sub-3-char token — the
    "title + 0 badge, no text" garbage cards. The operand drops those tokens
    (textmatch.fts_operand); a query left with NO operand (`ИИ-1.2` → ии/1/2) runs the
    literal contains scan directly — a deliberate mode, not a degradation.

    WHY exact: case-insensitive literal substring, no FTS/tokenization/stemming — the
    query is used VERBATIM (mirrors the agent's mode="exact").

    WHY under_document_id: the UI "In this document" narrow — same subtree semantics
    as semantic-search (root + descendants, uniform 404 on an unknown root), so the
    same label cannot mean two different scopes across the two modes.

    WHY the fallback: apply_schema tolerates per-statement DDL errors (db.py), so a node
    can legitimately be missing the index (parse regression / mid-build). If the FTS query
    raises, we re-run the literal scan so results never disappear — only ranking/speed
    degrade. No silent degradation of RESULTS (CLAUDE.md); the warning is logged.
    """
    await require_project_read(project_id, user)
    q_lower = q.lower()

    under = (under_document_id or "").strip() or None
    scope_clause = ""
    scope_params: dict = {}
    if under:
        root_row = await fetch_one("documents", under)
        if (
            not root_row
            or root_row.get("deleted_at")
            or root_row.get("project_id") != project_id
        ):
            raise HTTPException(404, "Search root document not found")
        from scope import subtree_doc_ids

        allowed = set(await subtree_doc_ids(under, project_id))
        scope_clause = "AND meta::id(id) IN $allowed "
        scope_params = {"allowed": list(allowed)}

    # ARCH(match-count): every returned card is ANCHORED — match_count counts the
    # anchor pattern (whole query verbatim, else the snippet-anchor ladder
    # 5→4→3 over content, then title) in title+content, and a row with NO anchor
    # is dropped, never returned with count 0. Why: a FTS stem/prefix fan-out hit
    # whose text carries none of the query tokens is indistinguishable from a wrong
    # result and rendered as a "0" garbage card.
    doc_rows: list | None = None
    if not exact:
        operand = fts_operand(q)
        if operand:
            try:
                doc_rows = await db.query(
                    _SEARCH_PROJECTION +
                    "WHERE project_id = $pid AND deleted_at IS NONE "
                    + scope_clause +
                    "AND (title @0@ $q OR content @1@ $q) "
                    "ORDER BY updated_at DESC LIMIT 50",
                    {"pid": project_id, "q": operand, **scope_params},
                )
                if isinstance(doc_rows, str):
                    raise RuntimeError(doc_rows)
            except Exception:
                # WHY: search never returns empty because the FTS index is absent —
                # fall back to the substring scan. apply_schema swallows a DDL failure,
                # so a missing index is a realistic node state, not an edge case.
                logger.warning("FTS search failed for project %s; falling back to string::contains", project_id, exc_info=True)
                doc_rows = None
    if doc_rows is None:
        # exact mode, empty FTS operand, or the FTS failure above.
        doc_rows = await _contains_scan(db, project_id, q_lower, scope_clause, scope_params)

    results = []
    for row in doc_rows or []:
        doc = serialize_record(row, "document_id")
        title = doc.get("title", "")
        content = doc.get("content", "") or ""
        title_lower = title.lower()
        content_lower = content.lower()
        blob = f"{title_lower} {content_lower}"
        if q_lower in blob:
            pattern: str | None = q_lower
        elif exact:
            # D1: exact is literal-only — never the anchor ladder.
            pattern = None
        else:
            pattern = (
                anchor_pattern(content_lower, q_lower, (5, 4, 3))
                or anchor_pattern(title_lower, q_lower, (5, 4, 3))
            )
        if not pattern:
            continue
        match_count = blob.count(pattern)
        is_ref = is_ref_row(doc)
        entry = {
            "document_id": doc["document_id"],
            "title": title,
            "match_count": match_count,
            "snippet": _build_snippet(content, pattern),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
        if is_ref:
            # WHY: reference_id alias on the response so clients that branch
            # on it continue to work; references are documents with is_reference=true.
            entry["reference_id"] = doc["document_id"]
            entry["document_id"] = doc.get("parent_id")
            entry["media_type"] = doc.get("media_type")
        results.append(entry)

    results.sort(key=lambda r: r["match_count"], reverse=True)
    return {"results": results}


def _build_snippet(content: str, query_lower: str, context_chars: int = 60) -> dict | None:
    """Extract a snippet around the first occurrence of query in content."""
    idx = content.lower().find(query_lower)
    if idx == -1:
        return None

    matched_text = content[idx : idx + len(query_lower)]

    raw_before = content[max(0, idx - context_chars) : idx]
    raw_after = content[idx + len(query_lower) : idx + len(query_lower) + context_chars]

    truncated_left = idx - context_chars > 0
    truncated_right = idx + len(query_lower) + context_chars < len(content)

    if truncated_left:
        space = raw_before.find(" ")
        raw_before = ("…" + raw_before[space + 1:]) if space != -1 else ("…" + raw_before)

    if truncated_right:
        space = raw_after.rfind(" ")
        raw_after = (raw_after[:space] + "…") if space != -1 else (raw_after + "…")

    return {"before": raw_before, "match": matched_text, "after": raw_after}


async def _audience_writer(project: dict | None, user: dict) -> tuple[bool, str | None]:
    """(is_admin, own group subject) of a caller allowed to write the audiences
    list — the owner or ANY admin, never is_project_root (a full non-owner
    member cannot touch the audiences)."""
    if is_instance_admin(user):
        return True, None
    if not project or project.get("owner_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Only the project owner can change the audiences")
    row = await fetch_one("users", user["user_id"]) or {}
    return False, own_group_subject(user["user_id"], row.get("role"), row.get("moderator_id"))


async def _require_live_moderator_subject(audience: str) -> None:
    """422 unless `audience` is 'mod:<uid>' of a live moderator row (admin writes)."""
    mod_uid = audience.removeprefix("mod:")
    row = await fetch_one("users", mod_uid) if mod_uid != audience else None
    if not row or row.get("deleted_at") or row.get("role") != "moderator":
        raise HTTPException(status_code=422, detail="a mod: audience subject must be a live moderator's mod:<uid>")


async def _guard_audiences(project: dict, new_audiences: list[dict], user: dict) -> None:
    """Set gate for the audiences list — a diff vs the stored list.

    # INVARIANT(security): ADDED/CHANGED rows are authorized per subject, REMOVED
    # rows are always allowed for the writer. Why: narrowing a project's audience
    # never weakens it — the owner revoking the admin-set 'public' row is a
    # deliberate ruling (the old is_public 403 for the owner is dropped;
    # _audience_writer already refused everyone else, so a full non-owner member
    # removes nothing) — while every add/change WIDENS it:
    #   'public' — an instance-wide broadcast is an operator decision:
    #     admin-only, and readonly|commentator only (write for everyone is
    #     never offered);
    #   admin 'mod:<uid>' — a live moderator row (a dead subject would read as
    #     granted while granting nothing); 'group:<id>' — an existing groups row;
    #   non-admin owner — ONLY their own group's subject (a foreign or
    #     admin-group subject opens the project past the owner's group, and an
    #     owner who moved groups must not raise what the OLD group gets —
    #     explicit binding).
    """
    is_admin, own = await _audience_writer(project, user)
    stored = {
        row.get("subject"): row.get("access_level")
        for row in (project.get("audiences") or [])
    }
    new = {row["subject"]: row["access_level"] for row in new_audiences}
    for subject, level in new.items():
        if stored.get(subject) == level:
            continue  # untouched row rides along in the full-list write
        if subject == PUBLIC_SUBJECT:
            if not is_admin:
                raise HTTPException(status_code=403, detail="Only an admin can set the 'public' audience")
            if level not in ("readonly", "commentator"):
                raise HTTPException(status_code=422, detail="The 'public' audience allows readonly or commentator only")
        elif is_admin:
            if subject.startswith("mod:"):
                await _require_live_moderator_subject(subject)
            else:
                group_uid = subject.removeprefix("group:")
                if not await fetch_one("groups", group_uid):
                    raise HTTPException(status_code=422, detail="audiences subject must be an existing group's group:<id>")
        elif subject != own:
            raise HTTPException(status_code=403, detail="Only your own group can be an audience")


@router.patch("/api/projects/{project_id}")
async def patch_project(project_id: str, body: PatchProject, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Update project fields (name, description, status, audiences, project_context, ref_image_preview). Requires full access or admin."""
    await require_admin_or_project_full(project_id, user)
    updates = body.model_dump(exclude_unset=True)
    if "audiences" in updates:
        project = await fetch_one("projects", project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        await _guard_audiences(project, updates["audiences"], user)
    if updates:
        set_parts = []
        params: dict = {"id": project_id}
        for k, v in updates.items():
            if v is None:
                set_parts.append(f"{k} = NONE")
            else:
                set_parts.append(f"{k} = ${k}")
                params[k] = v
        set_clause = ", ".join(set_parts)
        await db.query(
            f"UPDATE type::record('projects', $id) SET {set_clause}",
            params,
        )
        await emit("project_updated", project_id=project_id, updates=updates)
        # INVARIANT(security): an audiences change reaches OPEN collab sessions
        # at once — a project-wide access_changed (user_id=None) makes every
        # connected client recompute from the just-written rows. Why: the grants
        # are project-wide, so no single user can be named; and skipping this
        # is the pre-existing gap where an audience removal reached nobody
        # until reload.
        if "audiences" in updates:
            await emit("access_changed", project_id=project_id, user_id=None, access=None)
    updated = await fetch_one("projects", project_id)
    result = serialize_record(updated, "project_id") if updated else {}
    result["my_access"] = await get_project_access(project_id, user)
    result["is_owner_like"] = await is_project_root(updated or {}, user)
    return result


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Mark the project itself deleted — instantly; its contents keep their own state.

    # INVARIANT(persisted): deleting a project marks only the project row; documents,
    # references, chats, checkpoints and chunks keep their own deleted_at.
    # Why: soft delete is the general principle; keeping each document's own
    # state inside the project makes a later restore a one-field flip and keeps
    # "deleted by the user" distinguishable from "alive when the project died".
    """
    await require_admin_or_project_full(project_id, user)
    await soft_delete("projects", project_id)
    # Cascade the project's pending invites.
    await db.query(
        "DELETE pending_invites WHERE project_id = $pid", {"pid": project_id},
    )
    # INVARIANT: project delete stops its pipeline schedules. Why: the tick
    # (jobs.tasks.pipeline_schedule_tick_task) is the one ACTIVE consumer among
    # project children (api_keys/agent_configs are passive — gated by access
    # checks on read); its due predicate checks only the schedule row, so this
    # cascade is the single thing between a deleted project and eternal
    # dispatches. No restore path exists; a future one keeps schedules stopped
    # (documents stay soft-deleted today too).
    await db.query(
        "UPDATE pipeline_schedules SET deleted_at = time::now(), "
        "updated_at = time::now() WHERE project_id = $pid AND deleted_at IS NONE",
        {"pid": project_id},
    )
    await emit("project_deleted", project_id=project_id)
    return {"success": True}
