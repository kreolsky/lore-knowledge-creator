"""Phase-3 — config & knowledge as project documents (plan stage-1 §3.4).

The home agent's config is NOT files on disk — it is documents in a reserved
system subtree of the project, editable/visible as normal documents. This file
tests the backend pieces: lazy subtree init, system-prompt assembly (immutable
bootstrap tier + editable persona/rules/skills), the generalized agent_configs
coexistence (GAP-7), and the self-edit defense (system docs forced to confirm).

# ARCH (plan "agent-config-folder-subtrees"): rules / knowledge / skills are now
# three IDENTICAL folder-held subtrees. The folder carries content + children
# (plain docs, any depth, organization only); the whole live subtree is injected;
# the folder is the (append new here) target and is protected from deletion. No
# more duplicate seeded canonical children; no `# Your configuration` footer.
"""

import pytest
from agent.apply_policy import resolve_apply_mode
from agent_config import (
    _DEFAULT_KNOWLEDGE,
    _DEFAULT_RULES,
    BOOTSTRAP_SYSTEM_PROMPT,
    PROTECTED_SYSTEM_ROLES,
    SYSTEM_DOC_ROLES,
    build_agent_system_prompt,
    build_prompt_and_skill_docs,
    ensure_agent_system_docs,
    load_agent_system_docs,
    load_skills_subtree,
)

from db import create_record, get_db

# ─── Lazy system-subtree init ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "projects:⟨00d26d30-c049-480b-92fd-5f506efae456⟩",
    "projects:abc123",
    "",
])
async def test_ensure_refuses_a_record_link_project_id(bad, test_db):
    """A RecordID (or an empty id) must CRASH, not silently build a second skeleton.

    Observed on dev: one call with `projects:⟨uuid⟩` instead of the bare uuid
    created a full 10-doc ghost subtree that no project query could ever see
    (every loader filters `WHERE project_id = $pid` on the bare id). Nothing
    validated the input, so `_deterministic_id` happily wove the record-link into
    ten document ids. This is the guard that turns that into a loud failure.
    """
    with pytest.raises(ValueError, match="project_id"):
        await ensure_agent_system_docs(bad)


@pytest.mark.asyncio
async def test_ensure_creates_reserved_subtree(client, test_db, project_with_doc):
    pid, _idx, _admin = project_with_doc
    roles = await ensure_agent_system_docs(pid)

    # Required container/folder roles exist and are returned by id.
    for role in ("system_root", "system_prompt", "rules_folder", "skills_folder",
                 "knowledge_folder"):
        assert role in roles and roles[role], f"missing {role}"
    # NEW projects seed NO persona child — the system_prompt folder is empty
    # (Default / nothing injected). See plan "Locked decisions".
    assert "persona" not in roles
    # No more duplicate seeded rules/knowledge children — the folder IS the node.
    assert "rules" not in roles
    assert "knowledge" not in roles

    db = await get_db()
    docs = await db.query(
        "SELECT meta::id(id) AS id, system_role, is_system, parent_id, path, content "
        "FROM documents WHERE project_id = $pid AND is_system = true AND deleted_at IS NONE",
        {"pid": pid},
    )
    by_role = {d["system_role"]: d for d in docs}
    # Every system doc is marked is_system.
    assert all(d["is_system"] is True for d in docs)
    # No legacy rules/knowledge children were seeded (only folders now).
    assert "rules" not in by_role
    assert "knowledge" not in by_role
    # Container/folder roles live UNDER the root.
    root = by_role["system_root"]
    assert root["parent_id"] is None
    for child_role in ("system_prompt", "rules_folder", "skills_folder", "knowledge_folder"):
        assert by_role[child_role]["parent_id"] == root["id"]
    # Rules + knowledge folders are seeded WITH default content (the folder holds
    # the data now); skills folder is empty.
    assert by_role["rules_folder"]["content"] == _DEFAULT_RULES
    assert by_role["knowledge_folder"]["content"] == _DEFAULT_KNOWLEDGE
    assert by_role["skills_folder"]["content"] == ""


@pytest.mark.asyncio
async def test_ensure_is_idempotent(client, test_db, project_with_doc):
    pid, _idx, _admin = project_with_doc
    first = await ensure_agent_system_docs(pid)
    second = await ensure_agent_system_docs(pid)
    # Same doc ids — no duplication.
    assert first == second
    db = await get_db()
    docs = await db.query(
        "SELECT count() AS n FROM documents WHERE project_id = $pid AND is_system = true GROUP ALL",
        {"pid": pid},
    )
    assert docs[0]["n"] == len(first)


@pytest.mark.asyncio
async def test_ensure_resurrects_soft_deleted_folder_preserving_content(
    client, test_db, project_with_doc,
):
    """Resilient to a soft-deleted deterministic-id skeleton row (the original
    `Agent setup failed` root cause): ensure must UPDATE-resurrect the row
    (clear deleted_at, restore identity) and PRESERVE content (a folder may hold
    user edits), never CREATE-duplicate against the (project_id, path) unique
    index."""
    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    db = await get_db()
    rf_id = f"sys-rules_folder-{pid}"
    # Simulate the pre-delete-guard failure mode: user edits the folder, then it
    # is soft-deleted out-of-band, leaving a tombstoned row still holding its path.
    await db.query(
        "UPDATE type::record('documents', $id) SET content = $c",
        {"id": rf_id, "c": "USER-EDITED-RULES"},
    )
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": rf_id},
    )
    # ensure must resurrect (a bare CREATE would collide on the unique path index
    # and raise, which the Pi path swallowed as "Agent setup failed").
    await ensure_agent_system_docs(pid)
    rows = await db.query(
        "SELECT content, deleted_at, system_role, parent_id FROM type::record('documents', $id)",
        {"id": rf_id},
    )
    assert rows and rows[0]["deleted_at"] is None
    # Content preserved — resurrect never clobbers user edits.
    assert rows[0]["content"] == "USER-EDITED-RULES"
    # Identity restored.
    assert rows[0]["system_role"] == "rules_folder"
    assert rows[0]["parent_id"] is not None


# ─── System-prompt assembly ──────────────────────────────────────────────────


def test_build_prompt_has_immutable_bootstrap_tier():
    """The bootstrap tier is injected from CODE (immutable) — the agent can never
    edit it, only the persona/rules/skills docs beneath it (plan §3.4 edit tiers)."""
    prompt = build_agent_system_prompt({})
    assert prompt.startswith(BOOTSTRAP_SYSTEM_PROMPT)


def test_build_prompt_assembles_selected_persona_and_rules():
    """Only the SELECTED persona (matched by id in the persona list) is injected;
    Default/null injects none (plan: single injection point)."""
    personas = [
        {"id": "p1", "title": "Editor", "content": "You are a careful editor."},
        {"id": "p2", "title": "Coder", "content": "You are a terse coder."},
    ]
    prompt = build_agent_system_prompt(
        {"personas": personas,
         "rules_folder": {"id": "rf", "title": "Rules", "content": "Always confirm."},
         "rules_children": [{"id": "r1", "title": "Extra", "content": "Be brief."}]},
        selected_persona_id="p1",
    )
    assert "You are a careful editor." in prompt
    assert "You are a terse coder." not in prompt
    assert "Always confirm." in prompt
    assert "Be brief." in prompt


def test_build_prompt_null_selected_injects_no_persona_section():
    """No persona selected (Default) → no # Persona section at all, even when
    personas exist in the subtree."""
    personas = [{"id": "p1", "title": "Editor", "content": "You are a careful editor."}]
    prompt = build_agent_system_prompt({"personas": personas}, selected_persona_id=None)
    assert "# Persona" not in prompt
    assert "You are a careful editor." not in prompt


def test_build_prompt_unknown_selected_persona_injects_none():
    """A selected_persona_id that matches no persona child injects nothing (guards
    a stale id pointing at a deleted doc)."""
    personas = [{"id": "p1", "title": "Editor", "content": "You are a careful editor."}]
    prompt = build_agent_system_prompt({"personas": personas}, selected_persona_id="ghost-id")
    assert "# Persona" not in prompt


def test_build_prompt_emits_knowledge_section_when_content_present():
    """Knowledge is a folder-held subtree: the folder's own content renders under a
    `## title (append new here) (id: …)` header, every descendant under
    `## title (id: …)`, so content↔id is mappable for revisions."""
    prompt = build_agent_system_prompt({
        "knowledge_folder": {"id": "kf", "title": "Knowledge", "content": "Prefers metric units."},
        "knowledge_children": [{"id": "k2", "title": "Notes", "content": "Uses British spelling."}],
    })
    assert "# Knowledge" in prompt
    assert "## Knowledge (append new here) (id: kf)" in prompt
    assert "## Notes (id: k2)" in prompt
    assert "Prefers metric units." in prompt
    assert "Uses British spelling." in prompt


def test_build_prompt_omits_knowledge_section_when_empty():
    prompt = build_agent_system_prompt({"knowledge_folder": {"id": "kf", "title": "Knowledge", "content": ""}})
    assert "# Knowledge" not in prompt


def test_build_prompt_folder_is_append_target_no_footer():
    """The folder (always exists, protected from deletion) is the append target —
    its header carries `(append new here)`. The legacy `# Your configuration`
    footer and the duplicate seeded canonical child are both gone."""
    prompt = build_agent_system_prompt({
        "rules_folder": {"id": "rf", "title": "Rules", "content": "Some rules."},
        "rules_children": [{"id": "r1", "title": "Extra", "content": "More."}],
        "knowledge_folder": {"id": "kf", "title": "Knowledge", "content": "Some fact."},
        "knowledge_children": [{"id": "k2", "title": "Notes", "content": "Other."}],
    })
    # Footer block removed entirely (duplicated the ids already in section headers).
    assert "# Your configuration" not in prompt
    # Folder is the sole append target.
    assert "## Rules (append new here) (id: rf)" in prompt
    assert "## Knowledge (append new here) (id: kf)" in prompt
    # Descendants render plainly (no marker).
    assert "## Extra (id: r1)" in prompt
    assert "## Notes (id: k2)" in prompt
    # No legacy canonical child ids surface.
    assert "sys-rules" not in prompt
    assert "sys-knowledge" not in prompt


def test_build_prompt_each_node_carries_id_header_only_folder_marked():
    """Per-node `## title (id: …)` headers make content↔id mappable; only the
    folder carries the append marker (deterministic append target per turn)."""
    prompt = build_agent_system_prompt({
        "rules_folder": {"id": "rf", "title": "Rules", "content": "Be concise."},
        "rules_children": [
            {"id": "r1", "title": "Core", "content": "Metric units."},
            {"id": "r2", "title": "Style", "content": "British spelling."},
        ],
    })
    assert "## Rules (append new here) (id: rf)" in prompt
    assert "## Core (id: r1)" in prompt
    assert "## Style (id: r2)" in prompt
    # Exactly one append marker (the folder).
    assert prompt.count("(append new here)") == 1


def test_build_prompt_omits_your_configuration_block_when_docs_missing():
    prompt = build_agent_system_prompt({})
    assert "# Your configuration" not in prompt


def test_seeded_rules_route_a_learning_to_the_right_shape():
    """What stays in the SEEDED copy is only what a project may legitimately diverge
    on: where a new learning lands in this project's own Rules subtree.

    Rules = how this project wants to be worked on; Knowledge = what is true in it.
    The seeded body must tell the two apart and name the three shapes a learning can
    take, so the agent never has to guess between a bullet and a document.
    """
    low = _DEFAULT_RULES.lower()
    # The two folders are told apart, in the seeded body itself.
    assert "knowledge" in low
    # The three shapes, each named with the tool that makes it.
    assert "append new here" in low          # a bullet on the folder
    assert "create_document" in low          # a child document once it outgrows one
    assert "edit_document" in low            # revising what is already there
    # Editing by id, via the header the prompt prints for every node.
    assert "(id: ...)" in low or "(id:" in low


def test_default_knowledge_is_editable_prompt():
    """Knowledge folder ships a sensible editable default (regression guard)."""
    assert _DEFAULT_KNOWLEDGE.strip()


def test_build_prompt_skills_without_frontmatter_are_not_inlined():
    """Plan pi-native-skills (replaces the old 'load skills eagerly' contract):
    a skills-subtree doc WITHOUT frontmatter is NOT a skill, so it renders NEITHER
    in the prompt NOR as an inlined body — the catalog is published by dsh's tool-skill.
    Only frontmatter skills join <available_skills> (see
    test_build_prompt_renders_available_skills_index_not_bodies)."""
    prompt = build_agent_system_prompt({
        "skills_folder": {"id": "sf", "title": "Skills", "content": ""},
        "skills_children": [
            {"id": "s1", "title": "Summarize", "content": "How to summarize a document."},
            {"id": "s2", "title": "Link", "content": "How to add a reference."},
        ],
    })
    # No frontmatter → no valid skills → no # Skills section at all.
    assert "# Skills" not in prompt
    # And their bodies are certainly not inlined.
    assert "How to summarize a document." not in prompt
    assert "How to add a reference." not in prompt


def test_build_prompt_omits_skills_section_when_empty():
    """Empty skills folder + no descendants → no # Skills section."""
    prompt = build_agent_system_prompt({"skills_folder": {"id": "sf", "title": "Skills", "content": ""}})
    assert "# Skills" not in prompt


def test_build_prompt_injects_whole_subtree_any_depth():
    """Pure-fn contract: every descendant at ANY depth injects (tree = organization
    only). A grandchild nested two levels under the folder still renders."""
    prompt = build_agent_system_prompt({
        "rules_folder": {"id": "rf", "title": "Rules", "content": "FOLDER-CONTENT"},
        "rules_children": [
            {"id": "r1", "title": "Child", "content": "CHILD-CONTENT"},
            {"id": "r2", "title": "Grandchild", "content": "GRAND-CONTENT"},
        ],
    })
    assert "FOLDER-CONTENT" in prompt
    assert "CHILD-CONTENT" in prompt
    assert "GRAND-CONTENT" in prompt


@pytest.mark.asyncio
async def test_build_for_project_uses_selected_persona(client, test_db, project_with_doc):
    """The selected persona (session.system_prompt_id) is the one injected —
    session selection is the source of truth, threaded through
    build_prompt_and_skill_docs."""
    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    # User adds a persona child under the Personas folder and selects it.
    db = await get_db()
    folder = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND system_role = 'system_prompt'",
        {"pid": pid},
    )
    persona_id = f"sys-persona-{pid}"
    await create_record("documents", persona_id, {
        "project_id": pid, "parent_id": folder[0]["id"], "title": "Editor persona",
        "content": "Persona-from-editable-doc.", "path": ".lore/system/system_prompt/persona-1",
        "is_index": False, "is_system": True, "system_role": "persona",
    })
    prompt = (await build_prompt_and_skill_docs(pid, selected_persona_id=persona_id))[0]
    assert "Persona-from-editable-doc." in prompt
    assert prompt.startswith(BOOTSTRAP_SYSTEM_PROMPT)

    # Default (null selection) injects no persona even though one exists.
    prompt_default = (await build_prompt_and_skill_docs(pid, selected_persona_id=None))[0]
    assert "Persona-from-editable-doc." not in prompt_default


@pytest.mark.asyncio
async def test_load_and_build_injects_whole_live_subtree_any_depth(
    client, test_db, project_with_doc,
):
    """End-to-end: a child + grandchild nested under the rules folder (plain docs,
    is_system=false, no role tag) both inject on the next turn — the loader walks
    parent_id to any depth and drops the is_system/role filter for descendants."""
    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    db = await get_db()
    rf = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE system_role = 'rules_folder' AND project_id = $pid",
        {"pid": pid},
    )
    folder_id = rf[0]["id"]
    # Plain-doc child under the folder (NOT is_system, NOT role-tagged) — exactly
    # the case the old loader silently dropped.
    await create_record("documents", "rule-child-sub", {
        "project_id": pid, "parent_id": folder_id, "title": "Child",
        "content": "CHILD-LIVE-MARKER", "path": ".lore/system/rules_folder/child",
        "is_index": False,
    })
    # Grandchild under the child (depth 2) — still injects.
    await create_record("documents", "rule-grandchild-sub", {
        "project_id": pid, "parent_id": "rule-child-sub", "title": "Grandchild",
        "content": "GRAND-LIVE-MARKER", "path": ".lore/system/rules_folder/child/grand",
        "is_index": False,
    })
    docs = await load_agent_system_docs(pid)
    descendant_ids = [d["id"] for d in docs.get("rules_children", [])]
    assert "rule-child-sub" in descendant_ids
    assert "rule-grandchild-sub" in descendant_ids
    prompt = (await build_prompt_and_skill_docs(pid))[0]
    assert "CHILD-LIVE-MARKER" in prompt
    assert "GRAND-LIVE-MARKER" in prompt


@pytest.mark.asyncio
async def test_persona_is_direct_child_of_folder_positional(client, test_db, project_with_doc):
    """A persona = any DIRECT child document of the Personas folder, identified by
    parent_id (NOT by system_role/is_system). A plain doc (is_system=false,
    system_role=null) added under the Personas folder is selectable and injects
    when selected; default (null) injects none. A grandchild under a persona is
    NOT itself a persona (personas are flat siblings, depth-1 only)."""
    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    db = await get_db()
    sp_folder = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND system_role = 'system_prompt'",
        {"pid": pid},
    )
    folder_id = sp_folder[0]["id"]

    # Plain-doc direct child (NOT is_system, NOT role-tagged) — the exact prod
    # shape that the role-based loader silently dropped.
    persona_id = "persona-plain-child"
    await create_record("documents", persona_id, {
        "project_id": pid, "parent_id": folder_id, "title": "Plain persona",
        "content": "PLAIN-PERSONA-MARKER",
        "path": ".lore/system/system_prompt/plain-persona",
        "is_index": False,
    })
    # Grandchild under the persona (depth 2) — must NOT become a persona.
    await create_record("documents", "persona-grandchild", {
        "project_id": pid, "parent_id": persona_id, "title": "Nested note",
        "content": "GRANDCHILD-UNDER-PERSONA",
        "path": ".lore/system/system_prompt/plain-persona/note",
        "is_index": False,
    })

    docs = await load_agent_system_docs(pid)
    persona_ids = [p["id"] for p in docs.get("personas", [])]
    assert persona_id in persona_ids
    # Grandchild is depth-2 → not a flat sibling, never a persona.
    assert "persona-grandchild" not in persona_ids

    # Selected plain persona injects its own node content only.
    prompt = (await build_prompt_and_skill_docs(pid, selected_persona_id=persona_id))[0]
    assert "PLAIN-PERSONA-MARKER" in prompt
    # The grandchild's content is NOT injected (personas are not cumulative subtrees).
    assert "GRANDCHILD-UNDER-PERSONA" not in prompt
    assert prompt.startswith(BOOTSTRAP_SYSTEM_PROMPT)

    # Default (null selection) injects no persona even though one exists.
    prompt_default = (await build_prompt_and_skill_docs(pid, selected_persona_id=None))[0]
    assert "PLAIN-PERSONA-MARKER" not in prompt_default


# ─── Skeleton delete-guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_skeleton_folder_is_forbidden(
    client, test_db, admin_user, project_with_doc,
):
    """Skeleton system docs (is_system + protected role) cannot be deleted —
    deleting them left a tombstoned row holding its path, and the next agent turn
    crashed ensure on the unique index (the `Agent setup failed` root cause)."""
    pid, _idx, _admin = project_with_doc
    _, token = admin_user
    await ensure_agent_system_docs(pid)
    db = await get_db()
    rf = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE system_role = 'rules_folder' AND project_id = $pid",
        {"pid": pid},
    )
    folder_id = rf[0]["id"]
    r = await client.delete(f"/api/documents/{folder_id}", cookies={"lore_session": token})
    assert r.status_code == 403
    # Still present + live.
    rows = await db.query(
        "SELECT deleted_at FROM type::record('documents', $id)", {"id": folder_id},
    )
    assert rows and rows[0]["deleted_at"] is None


@pytest.mark.asyncio
async def test_delete_plain_child_of_folder_is_allowed(
    client, test_db, admin_user, project_with_doc,
):
    """The guard protects only the skeleton; plain-doc children of a folder remain
    deletable."""
    from db import create_record
    pid, _idx, _admin = project_with_doc
    _, token = admin_user
    await ensure_agent_system_docs(pid)
    db = await get_db()
    rf = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE system_role = 'rules_folder' AND project_id = $pid",
        {"pid": pid},
    )
    folder_id = rf[0]["id"]
    child_id = "rule-del-child-ok"
    await create_record("documents", child_id, {
        "project_id": pid, "parent_id": folder_id, "title": "Disposable",
        "content": "gone soon", "path": ".lore/system/rules_folder/disposable",
        "is_index": False,
    })
    r = await client.delete(f"/api/documents/{child_id}", cookies={"lore_session": token})
    assert r.status_code == 200
    rows = await db.query(
        "SELECT deleted_at FROM type::record('documents', $id)", {"id": child_id},
    )
    assert rows and rows[0]["deleted_at"] is not None


@pytest.mark.asyncio
async def test_batch_delete_rejects_skeleton_member(
    client, test_db, admin_user, project_with_doc,
):
    """Batch delete rejects (403) when any member is a protected skeleton doc."""
    pid, _idx, _admin = project_with_doc
    _, token = admin_user
    await ensure_agent_system_docs(pid)
    db = await get_db()
    rf = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE system_role = 'rules_folder' AND project_id = $pid",
        {"pid": pid},
    )
    folder_id = rf[0]["id"]
    plain_id = "rule-batch-plain"
    await create_record("documents", plain_id, {
        "project_id": pid, "parent_id": folder_id, "title": "Plain",
        "content": "x", "path": ".lore/system/rules_folder/plain",
        "is_index": False,
    })
    r = await client.post(
        "/api/documents/batch-delete",
        json={"document_ids": [plain_id, folder_id]},
        cookies={"lore_session": token},
    )
    assert r.status_code == 403
    # Neither deleted.
    for did in (plain_id, folder_id):
        rows = await db.query(
            "SELECT deleted_at FROM type::record('documents', $id)", {"id": did},
        )
        assert rows and rows[0]["deleted_at"] is None


def test_protected_roles_cover_skeleton_only():
    """Only the skeleton (root + folders + personas container) is protected;
    persona/legacy roles stay deletable."""
    for role in ("system_root", "system_prompt", "rules_folder", "skills_folder",
                 "knowledge_folder"):
        assert role in PROTECTED_SYSTEM_ROLES
    for role in ("persona", "rules", "knowledge", "skill"):
        assert role not in PROTECTED_SYSTEM_ROLES


# ─── Self-edit defense ───────────────────────────────────────────────────────


def test_system_doc_edit_policy_forces_confirm():
    """A system-doc edit is forced to confirm regardless of the apply preference —
    defense against silent privilege escalation when the agent self-edits its own
    rules. The `is_system` cell wins first, so the ui_preference is ignored."""
    assert resolve_apply_mode(
        ui_preference="auto",
        is_system=True,
    ).mode == "confirm"
    assert resolve_apply_mode(
        ui_preference="confirm",
        is_system=True,
    ).mode == "confirm"


def test_system_doc_edit_policy_passthrough_for_normal_docs():
    """For non-system docs the resolver passes the ui_preference through."""
    assert resolve_apply_mode(
        ui_preference="auto",
        is_system=False,
    ).mode == "auto"
    assert resolve_apply_mode(
        ui_preference="confirm",
        is_system=False,
    ).mode == "confirm"


# ─── Generalized agent_configs coexistence (GAP-7) ───────────────────────────


@pytest.mark.asyncio
async def test_agent_configs_kind_defaults_to_extractor(client, test_db, project_with_doc):
    """Existing extractor configs keep working; the table is generalized with a
    `kind` discriminator defaulting to 'extractor' (GAP-7 — coexist, not repurpose)."""
    pid, doc_id, _admin = project_with_doc
    await create_record("agent_configs", "ac-extractor-test", {
        "document_id": doc_id, "config_doc_id": doc_id, "target_doc_id": doc_id,
        "project_id": pid, "trigger_event": "transcription_complete",
    })
    db = await get_db()
    row = await db.query(
        "SELECT kind FROM agent_configs WHERE id = type::record('agent_configs', 'ac-extractor-test')"
    )
    assert row[0]["kind"] == "extractor"
    # The conversational kind is a valid, distinct coexisting value.
    await create_record("agent_configs", "ac-conv-test", {
        "document_id": doc_id, "config_doc_id": doc_id, "target_doc_id": doc_id,
        "project_id": pid, "trigger_event": "agent_turn", "kind": "conversational",
        "role": "system_prompt",
    })
    both = await db.query("SELECT kind FROM agent_configs WHERE project_id = $pid", {"pid": pid})
    kinds = {r["kind"] for r in both}
    assert kinds == {"extractor", "conversational"}


def test_system_doc_roles_are_reserved():
    # The reserved roles are the skeleton folder set; the legacy leaf roles
    # (persona/rules/skill/knowledge) were finalized away (plan
    # "agent-config-loader-bfs-legacy-cleanup"). system_role is only meaningful on
    # is_system docs.
    assert "system_root" in SYSTEM_DOC_ROLES
    assert "skills_folder" in SYSTEM_DOC_ROLES
    for legacy in ("persona", "rules", "skill", "knowledge"):
        assert legacy not in SYSTEM_DOC_ROLES


# ─── No Tools/Comfy chain ────────────────────────────────────────────────────
#
# The Comfy config is instance admin settings; a project seeds no Tools folder.

_TOOL_ROLES = ("tools_folder", "comfy", "comfy_prompt", "comfy_workflow",
               "comfy_settings")


@pytest.mark.asyncio
async def test_ensure_seeds_no_tools_comfy_chain(client, test_db, project_with_doc):
    pid, _idx, _admin = project_with_doc
    roles = await ensure_agent_system_docs(pid)
    assert not set(_TOOL_ROLES) & set(roles)
    db = await get_db()
    rows = await db.query(
        "SELECT system_role FROM documents WHERE project_id = $pid "
        "AND system_role IN $roles",
        {"pid": pid, "roles": list(_TOOL_ROLES)},
    )
    assert rows == []
    for role in _TOOL_ROLES:
        assert role not in SYSTEM_DOC_ROLES
        assert role not in PROTECTED_SYSTEM_ROLES


# ─── Pi-native skills: frontmatter index instead of inlining (plan pi-native-skills) ─
#
# A skill is a document in the skills_folder subtree carrying YAML frontmatter
# (name/description/tools). Stage 1: the system prompt carries an INDEX
# (<available_skills>: name/description/location) — bodies leave the context and
# arrive on demand via the dsh `skill` loader. Absent frontmatter = not a skill.

from agent_config import build_skill_docs, shipped_skill_docs  # noqa: E402

# Step 3 (collapse-the-editor-harness-layer): the backend ships RAW skill
# documents; parse + validation + overlay + off-switch + served gate live in
# the plugin (harness-driver/plugin/src/skills.ts, covered by skills.test.ts).
# What stays testable HERE is the LOOKUP: the wire's three layers and their
# raw content.

_FRONT_SKILL = (
    "---\n"
    "name: web-search\n"
    "description: use when the user asks to search the web or look something up online.\n"
    "tools:\n"
    "  - web_search\n"
    "---\n"
    "Fetch a URL or run a web query. Always cite the source URL in your answer."
)


def test_build_skill_docs_keeps_raw_heads_in_subtree_order():
    """Pure: from skills-subtree children [{id,title,content,parent_id}] build the
    project wire layer — every head RAW (the plugin decides skill-or-not), in
    subtree order, each head's descendants listed as material child_docs."""
    children = [
        {"id": "head-a", "title": "Web search", "parent_id": "folder", "content": _FRONT_SKILL},
        # A plain doc WITHOUT frontmatter rides too — skipping is the plugin's call.
        {"id": "doc-notes", "title": "Notes", "parent_id": "folder", "content": "prose only"},
        {"id": "mat1", "title": "Cheatsheet", "parent_id": "head-a", "content": "raw"},
        {"id": "mat2", "title": "Notes", "parent_id": "mat1", "content": "deeper"},
        # A descendant carrying its own frontmatter is STILL material, never a head.
        {"id": "nested", "title": "Nested", "parent_id": "mat2", "content": _FRONT_SKILL},
        {"id": "head-b", "title": "Memory", "parent_id": "folder", "content": "body-b"},
    ]
    docs = build_skill_docs(children)
    assert [d["location"] for d in docs] == ["head-a", "doc-notes", "head-b"]
    assert docs[0]["content"] == _FRONT_SKILL
    assert docs[1]["content"] == "prose only"
    assert docs[2]["content"] == "body-b"
    assert [c["id"] for c in docs[0]["child_docs"]] == ["mat1", "mat2", "nested"]
    assert docs[1]["child_docs"] == []
    assert docs[2]["child_docs"] == []


def test_shipped_skill_docs_read_every_configs_file_raw():
    """The shipped layer rides RAW with its stable "shipped:<stem>" marker — no
    copies are seeded into projects, and no parse happens Python-side."""
    docs = shipped_skill_docs()
    locations = {d["location"] for d in docs}
    assert "shipped:skill_memory_consolidation" in locations
    assert "shipped:skill_web_search" in locations
    for d in docs:
        assert d["location"].startswith("shipped:")
        # RAW: the frontmatter is still in the content (the plugin strips it).
        assert d["content"].startswith("---\n")
        assert d["child_docs"] == []


def test_build_prompt_skills_section_moved_to_driver_not_in_base():
    """The skills catalog is published by dsh's `tool-skill` from the turn
    payload's raw skills wire (fed through the plugin's lore-skills provider),
    NOT by this Python builder. The base system prompt carries NO skills
    section at all — neither an index nor inlined bodies — regardless of what
    the skills subtree holds."""
    prompt = build_agent_system_prompt({
        "skills_folder": {"id": "sf", "title": "Skills", "content": ""},
        "skills_children": [
            {"id": "s1", "title": "Web", "content": _FRONT_SKILL},
        ],
    })
    assert "# Skills" not in prompt
    assert "<available_skills>" not in prompt
    # Bodies are certainly not inlined.
    assert "Fetch a URL or run a web query." not in prompt


def test_build_prompt_skills_section_omitted_when_no_valid_skills():
    """No skills section is rendered for an empty skills folder (the driver adds the
    catalog only from the payload's skills wire, which is empty here)."""
    assert "# Skills" not in build_agent_system_prompt(
        {"skills_folder": {"id": "sf", "title": "Skills", "content": ""}}
    )


async def test_build_prompt_and_skill_docs_wires_all_three_layers(
    client, test_db, project_with_doc,
):
    """The turn wire: with an EMPTY skills subtree the project layer is [] and
    the shipped layer is the repo files; the shipped packs are pinned here
    (through the test-local frontmatter reader) because the pack is the only
    route the model gets to each capability."""
    import yaml as _yaml

    def _front(content: str) -> dict:
        assert content.startswith("---\n")
        end = content.index("\n---", 4)
        return _yaml.safe_load(content[4:end])

    pid, _idx, _admin = project_with_doc
    prompt, wire = await build_prompt_and_skill_docs(pid)
    assert prompt
    assert wire["project"] == []
    shipped = {(_front(d["content"]) or {}).get("name"): d for d in wire["shipped"]}
    assert set(shipped) == {
        "memory-consolidation", "web-search", "image-generation", "sandbox",
        "deep-research", "skill-authoring", "extractor-config",
    }
    meta = _front(shipped["memory-consolidation"]["content"])
    assert set(meta["tools"]) == {
        "consolidate_memory", "get_memory_facts",
        "apply_memory_verdicts", "next_reference", "get_fact_history",
        "reopen_consolidation",
    }
    assert _front(shipped["web-search"]["content"])["tools"] == ["web_search", "sandbox_bash"]
    assert _front(shipped["image-generation"]["content"])["tools"] == ["generate_image"]
    for d in shipped.values():
        assert d["location"].startswith("shipped:")
        assert d["child_docs"] == []
    # No skill documents were created in the project (the folder stays empty).
    assert build_skill_docs(await load_skills_subtree(pid)) == []


async def test_project_override_head_rides_the_wire_raw(client, test_db, project_with_doc):
    """A live head document rides the project layer RAW with its doc id as the
    location — the plugin's overlay makes it shadow the shipped skill of the
    same name (covered there)."""
    from db import create_record

    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    db = await get_db()
    folder = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND system_role = 'skills_folder' AND deleted_at IS NONE",
        {"pid": pid},
    )
    assert folder
    override = (
        "---\nname: web-search\ndescription: Project override — wins over the "
        "shipped file.\ntools: []\n---\n\nProject-specific search guidance.\n"
    )
    await create_record("documents", "skill-override-ws", {
        "project_id": pid, "parent_id": folder[0]["id"],
        "title": "web-search override", "content": override,
        "path": f"{folder[0]['id']}/skill-override-ws",
    })
    _prompt, wire = await build_prompt_and_skill_docs(pid)
    heads = [d for d in wire["project"] if d["location"] == "skill-override-ws"]
    assert len(heads) == 1
    assert heads[0]["content"] == override
    assert heads[0]["child_docs"] == []


async def test_deleted_copy_rides_the_tombstone_wire(client, test_db, project_with_doc):
    """F2 (the off-switch), LOOKUP half: deleting a direct child of the Skills
    folder puts its RAW content on the tombstones wire — the plugin parses the
    name out of it and suppresses the shipped skill (the CATALOG half is
    pinned in skills.test.ts)."""
    from db import create_record

    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    db = await get_db()
    folder = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND system_role = 'skills_folder' AND deleted_at IS NONE",
        {"pid": pid},
    )
    assert folder
    copy = (
        "---\nname: web-search\ndescription: the copy the user deletes\n"
        "tools: []\n---\n\nBody.\n"
    )
    await create_record("documents", "skill-off-ws", {
        "project_id": pid, "parent_id": folder[0]["id"],
        "title": "web-search", "content": copy,
        "path": f"{folder[0]['id']}/skill-off-ws",
    })
    # Live, the copy is a project head; the shipped layer still rides.
    _prompt, wire = await build_prompt_and_skill_docs(pid)
    assert any(d["location"] == "skill-off-ws" for d in wire["project"])
    assert wire["tombstones"] == []

    # The user deletes it → the copy moves from the project layer to the
    # tombstones, RAW.
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": "skill-off-ws"},
    )
    _prompt, wire = await build_prompt_and_skill_docs(pid)
    assert not any(d["location"] == "skill-off-ws" for d in wire["project"])
    assert copy in wire["tombstones"]


async def test_folder_head_wires_its_material(client, test_db, project_with_doc):
    """A folder-shaped skill: the head rides with its descendants as material
    child_docs (id + title), whatever their own content is."""
    from db import create_record

    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    db = await get_db()
    folder = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND system_role = 'skills_folder' AND deleted_at IS NONE",
        {"pid": pid},
    )
    assert folder
    await create_record("documents", "skill-folder-head", {
        "project_id": pid, "parent_id": folder[0]["id"],
        "title": "Guide", "content": _FRONT_SKILL,
        "path": f"{folder[0]['id']}/skill-folder-head",
    })
    await create_record("documents", "skill-folder-mat", {
        "project_id": pid, "parent_id": "skill-folder-head",
        "title": "Cheatsheet", "content": "raw material",
        "path": f"{folder[0]['id']}/skill-folder-head/skill-folder-mat",
    })
    _prompt, wire = await build_prompt_and_skill_docs(pid)
    head = next(d for d in wire["project"] if d["location"] == "skill-folder-head")
    assert head["child_docs"] == [{"id": "skill-folder-mat", "title": "Cheatsheet"}]
    # The material itself is NOT a head.
    assert not any(d["location"] == "skill-folder-mat" for d in wire["project"])



def test_every_shipped_model_facing_text_names_only_real_statuses():
    """The bootstrap is not the only text that reaches the model: the seeded Rules
    body and every shipped skill do too, and each is scanned against the SAME
    emitted-status set.

    Why the class and not the constant: the bootstrap alone was guarded, so
    `{status:"proposed"}` died in the code, failed here for the bootstrap, and
    survived untouched in `_DEFAULT_RULES` — where it had been COPIED into every
    project. A per-surface check is what makes the next retired status fail on
    every surface that teaches it.
    """
    import re

    from agent_config import _DEFAULT_RULES
    from agent_skills import shipped_skill_docs

    surfaces = {"_DEFAULT_RULES": _DEFAULT_RULES}
    for doc in shipped_skill_docs():
        # RAW content (frontmatter included) — the scan is over every byte the
        # plugin will eventually serve below the frontmatter split.
        surfaces[f"skill:{doc['location']}"] = doc["content"]

    emitted = _emitted_tool_api_statuses()
    for name, text in surfaces.items():
        taught = set(re.findall(r'status:\s*"([a-z_]+)"', text))
        assert taught <= emitted, (
            f"{name} teaches statuses no Tool-API path returns: "
            f"{sorted(taught - emitted)}"
        )


def _emitted_tool_api_statuses() -> set[str]:
    """The status literals the Tool-API / agent surfaces actually return."""
    import re
    from pathlib import Path

    import routes
    routes_dir = Path(routes.__file__).resolve().parent
    sources = list((routes_dir / "tool_api").rglob("*.py"))
    sources += list((routes_dir / "chat" / "agent").glob("*.py"))
    emitted = {
        m.group(1)
        for src in sources
        for m in re.finditer(r'"status":\s*"([a-z_]+)"', src.read_text())
    }
    assert emitted, "no Tool-API status literals found — the scan lost its target"
    return emitted


def test_bootstrap_prompt_names_only_statuses_the_tool_api_returns():
    """Every `status:"X"` the bootstrap prompt teaches must be a status some
    Tool-API surface actually returns.

    Derived on both sides — the prompt is scanned for the statuses it names and the
    Tool-API/agent modules for the statuses they emit — so a status the code stops
    returning (or starts returning) fails here instead of drifting silently into the
    model's contract. This is how `{status:"proposed"}` outlived the proposal cluster.
    """
    import re
    from pathlib import Path

    import routes
    from agent_config import BOOTSTRAP_SYSTEM_PROMPT
    routes_dir = Path(routes.__file__).resolve().parent
    sources = list((routes_dir / "tool_api").rglob("*.py"))
    sources += list((routes_dir / "chat" / "agent").glob("*.py"))
    emitted = {
        m.group(1)
        for src in sources
        for m in re.finditer(r'"status":\s*"([a-z_]+)"', src.read_text())
    }
    assert emitted, "no Tool-API status literals found — the scan lost its target"

    taught = set(re.findall(r'status:\s*"([a-z_]+)"', BOOTSTRAP_SYSTEM_PROMPT))
    assert taught, "the bootstrap prompt names no tool status — did the contract move?"
    assert taught <= emitted, (
        f"the bootstrap prompt teaches statuses no Tool-API path returns: "
        f"{sorted(taught - emitted)}"
    )
