"""BFS loader rewrite + legacy-role finalization (plan
"agent-config-loader-bfs-legacy-cleanup").

Two guarantees for a PURE tech-debt refactor:
  1. Byte-identical assembled Pi prompt (the golden test) — the loader's phase-1
     whole-project scan is replaced by a skeleton-index query + parent_id BFS, and
     the inert legacy role tags are finalized away; NONE of it may change the
     rendered prompt.
  2. Payload proportional to the config subtree, not the whole project — the BFS
     never issues the old `WHERE project_id = $pid ... ORDER BY sort_key` sweep and
     never returns unrelated project docs.

# ARCH: personas are depth-1 (direct children of the Personas folder); the three
# cumulative folders (rules/knowledge/skills) descend to any depth. The loader
# output keys are uniform: `<domain>_folder` (the node) + `<domain>_children`
# (its descendants) + `personas`.
"""

import os
import pathlib

import pytest
from agent_config import (
    BOOTSTRAP_SYSTEM_PROMPT,
    build_agent_system_prompt,
    build_prompt_and_skill_docs,
    ensure_agent_system_docs,
    load_agent_system_docs,
)

from db import create_record, get_db

_GOLDEN_PATH = pathlib.Path(__file__).parent / "fixtures" / "agent_config_golden_prompt.txt"


async def _seed_config_fixture(pid: str) -> dict[str, str]:
    """Seed a realistic agent-config subtree over the ensured skeleton:

      Personas folder:
        - persona A (plain doc, positional)      sort_key 'm'
        - persona B (LEGACY system_role='persona', is_system) sort_key 'k'
      Rules folder (default content) + children:
        - rule-1 (plain)                          sort_key 'k'
          - rule-1a grandchild (depth-2)          sort_key 'a'
        - rule-2 (LEGACY system_role='rules')     sort_key 'g'
      Knowledge folder (default content) + child:
        - know-1 (plain)                          sort_key 'a'
      Skills folder (empty) + child:
        - skill-1 (plain)                         sort_key 'a'

    Interleaved sort_keys across siblings of different parents exercise the
    per-level-order-vs-global-order equivalence (Decision 5). Returns id map.
    """
    await ensure_agent_system_docs(pid)
    db = await get_db()
    roles = await db.query(
        "SELECT meta::id(id) AS id, system_role FROM documents "
        "WHERE project_id = $pid AND is_system = true AND deleted_at IS NONE",
        {"pid": pid},
    )
    by_role = {r["system_role"]: r["id"] for r in roles}
    sp, rf, kf, sf = (
        by_role["system_prompt"], by_role["rules_folder"],
        by_role["knowledge_folder"], by_role["skills_folder"],
    )

    async def mk(did, parent, title, content, sk, *, is_system=False, role=None):
        await create_record("documents", did, {
            "project_id": pid, "parent_id": parent, "title": title,
            "content": content, "path": f".lore/system/x/{did}", "sort_key": sk,
            "is_index": False, "is_system": is_system, "system_role": role,
        })

    # Personas (depth-1 under the Personas folder). B carries the legacy tag.
    await mk("gf-persona-a", sp, "Persona A", "PERSONA-A-BODY", "m")
    await mk("gf-persona-b", sp, "Persona B", "PERSONA-B-BODY", "k",
             is_system=True, role="persona")
    # Rules subtree (folder ships _DEFAULT_RULES from ensure).
    await mk("gf-rule-1", rf, "Rule One", "RULE-ONE-BODY", "k")
    await mk("gf-rule-1a", "gf-rule-1", "Rule One-A", "RULE-1A-GRANDCHILD", "a")
    await mk("gf-rule-2", rf, "Rule Two", "RULE-TWO-BODY", "g",
             is_system=True, role="rules")
    # Knowledge subtree.
    await mk("gf-know-1", kf, "Know One", "KNOW-ONE-BODY", "a")
    # Skills subtree.
    await mk("gf-skill-1", sf, "Skill One", "SKILL-ONE-BODY", "a")
    return {"persona_a": "gf-persona-a", "persona_b": "gf-persona-b"}


# ─── Golden byte-equality (the safety net for both phases) ────────────────────


@pytest.mark.asyncio
async def test_golden_prompt_is_byte_identical(client, test_db, project_with_doc):
    """The assembled prompt for a rich fixture (skeleton + nested children + two
    personas + legacy-tagged rows mimicking migrated prod) must be BYTE-IDENTICAL
    across the refactor. The golden is captured from the pre-refactor code and
    committed; set LORE_REGEN_GOLDEN=1 to regenerate it (only when the format
    legitimately changes)."""
    pid, _idx, _admin = project_with_doc
    ids = await _seed_config_fixture(pid)
    prompt = (await build_prompt_and_skill_docs(
        pid, selected_persona_id=ids["persona_a"],
    ))[0]

    if os.environ.get("LORE_REGEN_GOLDEN"):
        _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN_PATH.write_text(prompt)
        pytest.skip("regenerated golden")

    assert _GOLDEN_PATH.exists(), "golden missing — regen with LORE_REGEN_GOLDEN=1"
    assert prompt == _GOLDEN_PATH.read_text()


# ─── Loader structure ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loader_keys_are_uniform(client, test_db, project_with_doc):
    """Loader output keys are the uniform `<domain>_folder` + `<domain>_children`
    (+ `personas`) set — no more `rules`/`knowledge`/`skill`/`persona` singulars."""
    pid, _idx, _admin = project_with_doc
    await _seed_config_fixture(pid)
    docs = await load_agent_system_docs(pid)
    for k in ("rules_folder", "knowledge_folder", "skills_folder",
              "rules_children", "knowledge_children", "skills_children", "personas"):
        assert k in docs, f"missing key {k}"
    # Old singular keys are gone.
    for old in ("rules", "knowledge", "skill", "persona"):
        assert old not in docs, f"legacy key {old} still present"


@pytest.mark.asyncio
async def test_personas_depth_1_only(client, test_db, project_with_doc):
    """Personas are the DIRECT children of the Personas folder (depth-1); a
    grandchild under a persona is never a persona."""
    pid, _idx, _admin = project_with_doc
    ids = await _seed_config_fixture(pid)
    # Add a grandchild under persona A.
    await create_record("documents", "gf-persona-a-child", {
        "project_id": pid, "parent_id": ids["persona_a"], "title": "Nested",
        "content": "NESTED-UNDER-PERSONA", "path": ".lore/system/x/pa-child",
        "sort_key": "a", "is_index": False,
    })
    docs = await load_agent_system_docs(pid)
    persona_ids = [p["id"] for p in docs["personas"]]
    assert ids["persona_a"] in persona_ids
    assert ids["persona_b"] in persona_ids  # legacy-tagged row resolves positionally
    assert "gf-persona-a-child" not in persona_ids


@pytest.mark.asyncio
async def test_rules_children_full_depth_in_sort_order(client, test_db, project_with_doc):
    """The rules folder descends to ANY depth; children appear in per-parent
    sort_key order (grandchild follows its parent in pre-order DFS)."""
    pid, _idx, _admin = project_with_doc
    await _seed_config_fixture(pid)
    docs = await load_agent_system_docs(pid)
    child_ids = [c["id"] for c in docs["rules_children"]]
    # rule-2 (sort_key 'g') precedes rule-1 (sort_key 'k'); grandchild follows rule-1.
    assert child_ids == ["gf-rule-2", "gf-rule-1", "gf-rule-1a"]


@pytest.mark.asyncio
async def test_empty_folders_yield_empty_children(client, test_db, project_with_doc):
    """A freshly-ensured project (no user children) → rules/knowledge AND skills
    children lists are all empty; the folders are present. The shipped skills
    live in the OVERLAY (backend/configs/, indexed at turn time) — nothing is
    seeded into the subtree anymore (plan pi-agent-layer-collapse step 6).

    The Personas folder seeds NOTHING: the seeded «Картограф» persona was RETIRED,
    so a fresh project ships ZERO personas out of the box. What the assertion
    protects is unchanged: nothing a USER did not create is loaded, and nothing here
    is injected under Default.
    """
    pid, _idx, _admin = project_with_doc
    await ensure_agent_system_docs(pid)
    docs = await load_agent_system_docs(pid)
    assert docs["rules_children"] == []
    assert docs["knowledge_children"] == []
    assert docs["skills_children"] == [], (
        "the skills subtree seeds nothing — shipped skills serve from the overlay"
    )


@pytest.mark.asyncio
async def test_legacy_tagged_rows_behave_identically(client, test_db, project_with_doc):
    """Legacy system_role tags (persona/rules) on migrated-prod-shaped rows do not
    change loading: the persona resolves positionally, the tagged rule descends as
    a normal child — both inject their content."""
    pid, _idx, _admin = project_with_doc
    ids = await _seed_config_fixture(pid)
    prompt = (await build_prompt_and_skill_docs(
        pid, selected_persona_id=ids["persona_b"],
    ))[0]
    assert "PERSONA-B-BODY" in prompt      # legacy-tagged persona injects when selected
    assert "RULE-TWO-BODY" in prompt       # legacy-tagged rule injects as a child


# ─── Payload boundedness (perf regression guard) ─────────────────────────────


@pytest.mark.asyncio
async def test_unrelated_docs_excluded_from_payload(client, test_db, project_with_doc):
    """N unrelated live project docs (outside the reserved subtree) never appear in
    the loader output — the BFS retrieves O(config), not O(project)."""
    pid, _idx, _admin = project_with_doc
    await _seed_config_fixture(pid)
    for i in range(5):
        await create_record("documents", f"unrelated-{i}", {
            "project_id": pid, "parent_id": None, "title": f"Doc {i}",
            "content": f"UNRELATED-{i}", "path": f"unrelated-{i}.md",
            "sort_key": f"z{i}", "is_index": False,
        })
    docs = await load_agent_system_docs(pid)
    all_ids = set()
    for v in docs.values():
        for node in (v if isinstance(v, list) else [v]):
            if isinstance(node, dict):
                all_ids.add(node.get("id"))
    for i in range(5):
        assert f"unrelated-{i}" not in all_ids


@pytest.mark.asyncio
async def test_loader_issues_no_whole_project_scan(monkeypatch, project_with_doc):
    """Query-shape guard: the loader must NOT issue the old whole-project slim scan
    (`WHERE project_id = $pid AND deleted_at IS NONE ORDER BY sort_key`). It uses a
    skeleton lookup (`is_system = true`) + per-level `parent_id IN $ids` BFS.

    White-box by design: coupling to the SQL text is intentional — this test exists
    precisely to fail if anyone reintroduces the O(project) sweep the perf refactor
    removed. Behavioral coverage of the same guarantee lives in
    test_unrelated_docs_excluded_from_payload.

    The monkeypatch targets `agent_config_load` (where load_agent_system_docs now
    lives after the agent_config.py module split) — `get_db` is resolved in the
    module that defines the function, so patching the façade's re-export has no
    effect on the name lookup inside it."""
    from unittest.mock import AsyncMock

    import agent_config_load

    pid = "shape-pid"
    seen: list[str] = []

    # Minimal canned tree: root + 4 folders (skeleton), one rule child, one persona.
    def norm(sql):
        return " ".join(sql.split())

    async def fake_query(sql, params=None):
        s = norm(sql)
        seen.append(s)
        if "is_system = true" in s and "SELECT meta::id(id) AS id, system_role" in s:
            return [
                {"id": "root", "system_role": "system_root", "title": "Agent System"},
                {"id": "sp", "system_role": "system_prompt", "title": "Personas"},
                {"id": "rf", "system_role": "rules_folder", "title": "Rules"},
                {"id": "kf", "system_role": "knowledge_folder", "title": "Knowledge"},
                {"id": "sf", "system_role": "skills_folder", "title": "Skills"},
            ]
        if "parent_id IN $ids" in s:
            fids = set(params.get("ids", []))
            rows = []
            if "sp" in fids:
                rows.append({"id": "p1", "system_role": None, "title": "P", "sort_key": "a", "parent_id": "sp"})
            if "rf" in fids:
                rows.append({"id": "r1", "system_role": None, "title": "R", "sort_key": "a", "parent_id": "rf"})
            return rows
        if "content" in s and "id IN" in s:
            return [{"id": "r1", "content": "X"}, {"id": "p1", "content": "Y"}]
        return []

    db = AsyncMock()
    db.query = fake_query
    monkeypatch.setattr(agent_config_load, "get_db", AsyncMock(return_value=db))

    await load_agent_system_docs(pid)

    # The forbidden whole-project scan: project-scoped + deleted filter + ORDER BY
    # sort_key but WITHOUT a parent_id / is_system narrowing.
    for s in seen:
        forbidden = (
            "project_id = $pid AND deleted_at IS NONE ORDER BY sort_key" in s
            and "parent_id" not in s
            and "is_system" not in s
        )
        assert not forbidden, f"whole-project scan issued: {s}"
    # And the BFS query shape is actually used.
    assert any("parent_id IN $ids" in s for s in seen)


# ─── build_agent_system_prompt with the uniform keys (pure fn) ───────────────────


def test_build_prompt_reads_uniform_children_keys():
    """The pure assembler reads `<domain>_children` / `personas` — the renamed
    internal contract with the loader. Rules + Knowledge render inline; Skills does
    NOT (dsh's `tool-skill` publishes the catalog from the payload's skills[],
    so a skill BODY is never in this base string)."""
    prompt = build_agent_system_prompt({
        "personas": [{"id": "p1", "title": "Ed", "content": "PERSONA-BODY"}],
        "rules_folder": {"id": "rf", "title": "Rules", "content": "RULES-FOLDER"},
        "rules_children": [{"id": "r1", "title": "Extra", "content": "RULES-CHILD"}],
        "knowledge_folder": {"id": "kf", "title": "Knowledge", "content": "KN-FOLDER"},
        "knowledge_children": [{"id": "k1", "title": "N", "content": "KN-CHILD"}],
        "skills_folder": {"id": "sf", "title": "Skills", "content": ""},
        "skills_children": [{"id": "s1", "title": "S", "content": "SKILL-CHILD"}],
    }, selected_persona_id="p1")
    assert prompt.startswith(BOOTSTRAP_SYSTEM_PROMPT)
    for marker in ("PERSONA-BODY", "RULES-FOLDER", "RULES-CHILD", "KN-FOLDER", "KN-CHILD"):
        assert marker in prompt
    # Skills bodies leave the base prompt (index-only; delivered on demand via
    # the dsh `skill` loader) — so a skill body is NOT in the assembled prompt.
    assert "SKILL-CHILD" not in prompt
    assert "# Skills" not in prompt
