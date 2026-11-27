"""Agent config as project documents.

The home agent's config is NOT files on disk — it is documents in a reserved
system subtree of the project, editable/visible as normal documents. This file
tests the backend pieces: lazy subtree init, system-prompt assembly (immutable
bootstrap tier + editable persona/rules/skills), the generalized agent_configs
coexistence (GAP-7), and the self-edit defense (system docs forced to confirm).

# ARCH: rules / skills are two IDENTICAL folder-held subtrees. The folder carries
# content + children (plain docs, any depth, organization only); the whole live
# subtree is injected; the folder is the (append new here) target and is protected
# from deletion.
"""

import pytest
import settings
from agent_config import (
    DEFAULT_BOOTSTRAP_PROMPT,
    build_prompt_and_skill_docs,
    ensure_agent_system_docs,
    load_skills_subtree,
)
from agent_skills import (
    EMBED_SCHEMES_TOKEN,
    MAIN_EDIT_SKILL,
    embed_token_problem,
    frontmatter_name,
    project_embed_schemes,
    reset_shipped_skills_cache,
    shipped_skill_docs,
)
from settings_registry import ConfigError

from db import create_record, get_db
from transclusion_grammar import render_embed_schemes

# ─── Lazy system-subtree init ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_first_ensures_both_succeed(client, test_db, project_with_doc):
    """Two first turns of a fresh project arrive together (two quick sends):
    both lazy-inits find no skeleton and both CREATE it — the loser must take
    the winner's row, not fail the turn ("Agent setup failed")."""
    import asyncio

    pid, _idx, _admin = project_with_doc
    first, second = await asyncio.gather(
        ensure_agent_system_docs(pid), ensure_agent_system_docs(pid),
    )
    assert first == second
    db = await get_db()
    docs = await db.query(
        "SELECT count() AS n FROM documents WHERE project_id = $pid AND is_system = true GROUP ALL",
        {"pid": pid},
    )
    assert docs[0]["n"] == len(first)


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


# ─── Pi-native skills: frontmatter index instead of inlining (plan pi-native-skills) ─
#
# A skill is a document in the skills_folder subtree carrying YAML frontmatter
# (name/description/tools). Stage 1: the system prompt carries an INDEX
# (<available_skills>: name/description/location) — bodies leave the context and
# arrive on demand via the dsh `skill` loader. Absent frontmatter = not a skill.

from agent_config import build_skill_docs  # noqa: E402

# Step 3 (collapse-the-editor-harness-layer): the backend ships RAW skill
# documents; parse + validation + overlay + off-switch + served gate live in
# the plugin (harness-driver/plugin/src/skills.ts, covered by skills.test.ts).
# What stays testable HERE is the LOOKUP: the wire's three layers and their
# raw content.


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
    prompt, wire, _persona = await build_prompt_and_skill_docs(pid)
    assert prompt
    assert wire["project"] == []
    shipped = {(_front(d["content"]) or {}).get("name"): d for d in wire["shipped"]}
    assert set(shipped) == {
        "memory", "web-search", "image-generation", "sandbox",
        "deep-research", "skill-authoring", "extractor-config", "chat-recall",
        "digest-config", "secrets", "history", "md-folder", "tables", "structure",
        "pipelines", "tasks",
        # The document tools: the default skills.
        "main-read", "main-edit", "main-files", "main-config",
    }
    # The task pack: the task loop's three tools — pinned like every other pack.
    assert _front(shipped["tasks"]["content"])["tools"] == [
        "find_documents", "next_task", "claim_task", "set_task_status",
    ]
    assert _front(shipped["secrets"]["content"])["tools"] == [
        "list_secrets", "http_request",
    ]
    # The pipeline pack (plan pipeline-agent-tools): binding consent + the run
    # log read the agent verifies with — pinned like every other pack.
    assert _front(shipped["pipelines"]["content"])["tools"] == [
        "list_pipelines", "bind_pipeline", "unbind_pipeline",
        "run_pipeline", "pipeline_runs",
    ]
    meta = _front(shipped["memory"]["content"])
    assert set(meta["tools"]) == {
        "consolidate_memory", "get_memory_facts",
        "apply_memory_verdicts", "next_reference", "get_fact_history",
        "reopen_consolidation", "save_chat_facts", "set_fact_anchor",
    }
    assert _front(shipped["web-search"]["content"])["tools"] == [
        "web_search", "web_fetch", "sandbox_bash",
        "sandbox_run_status", "sandbox_run_kill",
    ]
    assert _front(shipped["image-generation"]["content"])["tools"] == ["generate_image"]
    # The structure pack: the chat move rides it — structure, not recall.
    assert _front(shipped["structure"]["content"])["tools"] == [
        "move_document", "get_links", "reprocess_reference", "move_chat",
        "set_chat_persona",
    ]
    # The default skills carry the document tools that were the always-active
    # core — reading split from writing, so a persona can start with one.
    assert _front(shipped["main-read"]["content"])["tools"] == [
        "read_document", "get_project_structure", "search_materials",
    ]
    assert _front(shipped["main-edit"]["content"])["tools"] == [
        "create_document", "edit_document", "append_to_document", "rename_document",
    ]
    assert _front(shipped["main-files"]["content"])["tools"] == ["import_file", "view_reference_image"]
    assert _front(shipped["main-config"]["content"])["tools"] == ["save_skill"]
    # Nothing served is left outside a skill: the plugin's `active tools
    # resolved` log must count `core: 0` (harness-driver skills.ts
    # computeCoreTools = served minus the packed tools) — derived on both sides.
    from agent_tools.registry import agent_entries

    packed = {t for d in shipped.values() for t in (_front(d["content"]) or {}).get("tools") or []}
    assert {e.name for e in agent_entries()} - packed == set()
    # The wire is the one place the embed token is projected: no skill body
    # carries it, and the embedding instructions carry the live grammar.
    assert render_embed_schemes() in shipped["main-edit"]["content"]
    for d in (*wire["shipped"], *wire["instance"], *wire["project"]):
        assert EMBED_SCHEMES_TOKEN not in d["content"], d["location"]
    for d in shipped.values():
        assert d["location"].startswith("shipped:")
        assert d["child_docs"] == []
    # No skill documents were created in the project (the folder stays empty).
    assert build_skill_docs(await load_skills_subtree(pid)) == []


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
    routes_dir = Path(routes.__file__).resolve().parent
    sources = list((routes_dir / "tool_api").rglob("*.py"))
    sources += list((routes_dir / "chat" / "agent").glob("*.py"))
    emitted = {
        m.group(1)
        for src in sources
        for m in re.finditer(r'"status":\s*"([a-z_]+)"', src.read_text())
    }
    assert emitted, "no Tool-API status literals found — the scan lost its target"

    taught = set(re.findall(r'status:\s*"([a-z_]+)"', DEFAULT_BOOTSTRAP_PROMPT))
    assert taught, "the bootstrap prompt names no tool status — did the contract move?"
    assert taught <= emitted, (
        f"the bootstrap prompt teaches statuses no Tool-API path returns: "
        f"{sorted(taught - emitted)}"
    )


# ─── The two admin tiers and the embed token ────────────────────────────────


# TEST-WHY: recurring-regression — agent_config.py's prompt assembly is a repeatedly patched area; the floor-first order and the one droppable tier are what every later patch must keep.
@pytest.mark.asyncio
async def test_a_persona_drops_the_base_persona_and_never_the_floor(
    test_db, project_with_doc,
):
    """`base_prompt: false` removes the base persona from that persona's prompt
    and nothing else: the floor still opens it, the persona's own text and the
    rules follow. The false-green half is the persona WITHOUT the key: its
    prompt carries the base persona."""
    pid, _idx, _admin = project_with_doc
    roles = await ensure_agent_system_docs(pid)
    for pid_doc, front in (("persona-own-role", "base_prompt: false\n"), ("persona-defaults", "")):
        await create_record("documents", pid_doc, {
            "project_id": pid, "parent_id": roles["system_prompt"],
            "title": pid_doc, "content": f"---\n{front}---\nROLE-OF-{pid_doc}",
            "path": f".lore/system/system_prompt/{pid_doc}", "is_index": False,
        })
    floor = (await settings.get("AGENT_BOOTSTRAP_PROMPT")).rstrip()
    base = (await settings.get("AGENT_BASE_PERSONA_PROMPT")).rstrip()

    prompt, _wire, persona = await build_prompt_and_skill_docs(pid, selected_persona_id="persona-own-role")
    assert prompt.startswith(f"{floor}\n\n# Persona\nROLE-OF-persona-own-role")
    assert base not in prompt, "a persona with base_prompt: false carries no base persona"
    assert (persona.base_prompt, persona.preload_defaults) == (False, True)

    prompt, _wire, persona = await build_prompt_and_skill_docs(pid, selected_persona_id="persona-defaults")
    assert prompt.startswith(f"{floor}\n\n{base}\n\n# Persona\nROLE-OF-persona-defaults")
    assert persona.base_prompt is True, "without the key the base persona is kept"


# TEST-WHY: pure-logic-edge — the embed token moved into the main-edit body; a shipped file or an instance override without exactly one token must fail loudly at read or at write, or the model is taught a frozen grammar or none.
def test_the_embed_token_lives_in_the_main_edit_body_on_every_path(tmp_path):
    from fastapi import HTTPException
    from routes.admin_skills import (
        SkillUpdate,
        _refuse_broken_main_edit,
        _resolved_fields,
    )

    shipped = {frontmatter_name(d["content"]): d["content"] for d in shipped_skill_docs()}
    good = shipped[MAIN_EDIT_SKILL]
    assert good.count(EMBED_SCHEMES_TOKEN) == 1, "the shipped body carries the token once"
    assert embed_token_problem(good) is None
    twice = good.replace("level deep.", f"level deep {EMBED_SCHEMES_TOKEN}.")
    for bad in ("no token here", "", twice):
        problem = embed_token_problem(bad)
        assert problem is not None and EMBED_SCHEMES_TOKEN in problem, repr(bad)

    # The wire projection is a copy: the stored / shipped text keeps the token.
    raw = [{"location": "shipped:x", "content": f"a {EMBED_SCHEMES_TOKEN} b", "child_docs": []}]
    out = project_embed_schemes(raw)
    assert out[0]["content"] == f"a {render_embed_schemes()} b"
    assert raw[0]["content"] == f"a {EMBED_SCHEMES_TOKEN} b"
    assert project_embed_schemes([{"content": "no token", "location": "l"}])[0] == {
        "content": "no token", "location": "l"}, "an untouched doc is the same object"

    # A broken SHIPPED file fails the read; the fixed one reads (and is not
    # cached from the failed attempt).
    reset_shipped_skills_cache()
    try:
        path = tmp_path / "skill_main_edit.md"
        path.write_text(good.replace(EMBED_SCHEMES_TOKEN, "a hand-written list"), encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            shipped_skill_docs(str(tmp_path))
        assert "skill_main_edit.md" in str(exc.value) and EMBED_SCHEMES_TOKEN in str(exc.value)
        path.write_text(good, encoding="utf-8")
        assert [d["location"] for d in shipped_skill_docs(str(tmp_path))] == ["shipped:skill_main_edit"]
    finally:
        reset_shipped_skills_cache()

    # An instance OVERRIDE is refused at the write, whatever path writes it;
    # the unchanged body and any other skill pass.
    hand_written = good.replace(EMBED_SCHEMES_TOKEN, "- doc <id>")
    with pytest.raises(HTTPException) as exc:
        _resolved_fields(MAIN_EDIT_SKILL, SkillUpdate(content=hand_written), None, {MAIN_EDIT_SKILL: good})
    assert exc.value.status_code == 422 and EMBED_SCHEMES_TOKEN in exc.value.detail
    assert _resolved_fields(MAIN_EDIT_SKILL, SkillUpdate(content=good), None, {MAIN_EDIT_SKILL: good}) == (
        good, True)
    # The row is disabled by the same PUT: a tombstone serves nothing, so it is not checked.
    assert _resolved_fields(MAIN_EDIT_SKILL, SkillUpdate(enabled=False), None, {MAIN_EDIT_SKILL: good}) == (
        None, False)
    other = "---\nname: other\ndescription: d\n---\nno token"
    assert _resolved_fields("other", SkillUpdate(content=other), None, {}) == (other, True)
    # The zip import's entry point takes the same two: nothing to serve, or another skill.
    assert _refuse_broken_main_edit(MAIN_EDIT_SKILL, None) is None
    assert _refuse_broken_main_edit("other", "no token") is None
