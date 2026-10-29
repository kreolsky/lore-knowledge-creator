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


import pytest
from agent_config import (
    ensure_agent_system_docs,
    load_agent_system_docs,
)

from db import create_record, get_db


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
