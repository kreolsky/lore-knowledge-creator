"""memory-consolidation skill — the tool pack + the match description.

Plan `.kilo/plans/skills-fixes-and-persona-retirement.md` Stage 2 (D3 + D4). With the
«Картограф» persona retired, this skill is the ONLY route to consolidation: a missed
description match makes the capability unreachable for that turn, and the pack is what
the driver carves out of core + activates on a `skill` load (read off the dsh session
log). The pack list is therefore
asserted against the SERVED toolset (`await agent_toolset()`), not against a literal copy —
a renamed tool (or a pack member the toolset dropped) fails here rather than shipping a
skill whose pack activates nothing.
"""


# The skill document, READ once for the whole module (RAW — the production
# parse lives in the plugin; the fields come from the test-side reader). The
# configs dir resolves via the MODULE's location (repo-root walking breaks in
# the test container).
import agent_skills as _askills
import pytest
from agent.tools import agent_toolset
from helpers import skill_frontmatter

_CONTENT = (_askills.CONFIGS_DIR / "skill_memory_consolidation.md").read_text(
    encoding="utf-8"
).strip()
_SKILL = skill_frontmatter(_CONTENT)

# The consolidation loop is a closed loop over these tools — locked here so a
# dropped/renamed member is caught at the pack-defining layer too, not only at the
# served-toolset layer. `get_fact_history` is the explicit-request channel for a
# claim's supersede chain (the marker on the served claim says "revised"; this tool
# returns the retired wording on demand). `reopen_consolidation` clears the consumed-
# stamps so a target can be walked again under a new accent or model — same pack
# (the only route to consolidation), but never a step of the normal loop.
_EXPECTED_PACK = {
    "consolidate_memory",
    "get_memory_facts",
    "apply_memory_verdicts",
    "next_reference",
    "get_fact_history",
    "reopen_consolidation",
}


def test_memory_consolidation_parses_as_a_skill():
    """The seeded document must be a valid skill (frontmatter + valid name/description)
    — a malformed frontmatter would make it invisible to the index entirely."""
    assert _SKILL is not None, "memory-consolidation must parse as a valid skill"
    assert _SKILL["name"] == "memory-consolidation"


def test_memory_consolidation_carries_its_whole_tool_pack():
    """The pack is present and is exactly the consolidation loop plus its history
    reader. Walking the PARSED frontmatter (not a literal string match) so a
    reordered/edited tools list is read structurally."""
    assert _SKILL is not None
    assert set(_SKILL["tools"]) == _EXPECTED_PACK, (
        f"expected {sorted(_EXPECTED_PACK)}, got {sorted(_SKILL['tools'])}"
    )


async def test_every_pack_tool_is_served():
    """Every pack member is part of the SERVED toolset — a renamed tool, or a pack
    tool the toolset dropped, fails here. The pack is the only route to consolidation
    now that the persona is gone, so an unserved member makes the loop uncallable."""
    assert _SKILL is not None
    served = {t["function"]["name"] for t in await agent_toolset()}
    for pack_tool in _SKILL["tools"]:
        assert pack_tool in served, (
            f"pack tool {pack_tool!r} is not served by await agent_toolset() — "
            "a renamed tool makes consolidation unreachable"
        )


def test_description_names_user_verbs_in_both_languages():
    """The description is the SOLE auto-match signal (the persona no longer falls back
    to it), so it must name the verbs users actually type — in both languages the
    project is used in. A description that omits them is a missed match = unreachable
    capability (D3)."""
    assert _SKILL is not None
    lowered = _SKILL["description"].lower()
    # English verbs the user types.
    for verb in ("consolidate", "merge", "clean up", "verify"):
        assert verb in lowered, f"description omits the verb '{verb}'"
    # Russian: the word for memory + a consolidation verb stem.
    assert "памят" in lowered, "description omits Russian 'память'"
    assert "консолид" in lowered, "description omits a Russian consolidate stem"


# ─── The two defaults (target + breadth), over the SERVED skill ───────────────
#
# The served body for an empty project IS the shipped file's body (the overlay
# resolves it plugin-side; backend-side the raw file is the same bytes minus
# frontmatter), so the prose is asserted over the shipped document directly.


@pytest.mark.asyncio
async def test_served_body_names_the_target_and_breadth_defaults(
    client, test_db, project_with_doc,
):
    """The two defaults are prose the model reads: no document named ⇒ the document
    the chat is open on (the `# Current document` prompt section's document); the
    breadth ⇒ the target's subtree, one closed run per document, with
    "this document only" narrowing it to a single run. Each phrase is asserted
    verbatim — the section name is the anchor that ties the default to the prompt,
    and the narrowing phrase is what the user types to opt out of the walk."""
    body = _SKILL["body"]
    assert "# Current document" in body, (
        "body must name the # Current document anchor (the default target)"
    )
    assert "get_project_structure" in body, (
        "body must name get_project_structure (the default breadth's walker)"
    )
    assert "this document only" in body, (
        "body must name the 'this document only' narrowing phrase"
    )
