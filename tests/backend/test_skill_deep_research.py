"""deep-research skill — the pack it activates, and the core tools its prose commands.

The body walks the agent through four phases and NAMES tools in every one of them:
`search_materials` in recon, `create_document` after the outline, `append_to_document`
per section, `edit_document` at compile. Only `web_search` and `sandbox_bash` are
packed; the rest must be reachable WITHOUT a skill load, because the driver carves core
out as served-minus-every-pack (harness-driver/plugin/src/skills.ts). A tool the prose
commands that some other skill packs would be invisible when this skill runs — the
model would be told to call something it cannot see.

Both surfaces are derived: the pack from the shipped file, core from the served toolset
minus every seeded pack. Nothing here mirrors a literal.
"""


import agent_skills
from agent.tools import agent_toolset
from helpers import skill_frontmatter

_CONTENT = (agent_skills.CONFIGS_DIR / "skill_deep_research.md").read_text(encoding="utf-8").strip()
_SKILL = skill_frontmatter(_CONTENT)

# The pack: find pages (web_search) and read the ones a snippet cannot answer
# (sandbox_bash). Everything the four phases do to the PROJECT is core.
_EXPECTED_PACK = {"web_search", "sandbox_bash"}

# Tools the body instructs the agent to call while this skill is loaded, which the
# skill does NOT pack — so each one has to be core.
_COMMANDED_CORE = {
    "search_materials",
    "create_document",
    "append_to_document",
    "edit_document",
}


# Served by the harness itself (dsh tool-web, harness-driver/home/cordis.patch.yml),
# never by the backend — so it is absent from agent_toolset and added here.
_HARNESS_SERVED = {"web_search"}


async def _served() -> set[str]:
    """The served surface with the env gates forced open: CI ships no sandbox host,
    and the contract under test is pack↔core consistency, not the gate."""
    from helpers import pinned_tool_gates

    with pinned_tool_gates(sandbox=True):
        return {t["function"]["name"] for t in await agent_toolset()} | _HARNESS_SERVED


def _every_pack() -> set[str]:
    packed: set[str] = set()
    for path in sorted(agent_skills.CONFIGS_DIR.glob("skill_*.md")):
        packed.update(skill_frontmatter(path.read_text(encoding="utf-8"))["tools"])
    return packed


def test_deep_research_parses_as_a_skill():
    assert _SKILL is not None, "deep-research must parse as a valid skill"
    assert _SKILL["name"] == "deep-research"


def test_deep_research_carries_exactly_the_search_and_console_pair():
    assert set(_SKILL["tools"]) == _EXPECTED_PACK, (
        f"expected {sorted(_EXPECTED_PACK)}, got {sorted(_SKILL['tools'])}"
    )


async def test_every_pack_tool_is_served():
    served = await _served()
    for pack_tool in _SKILL["tools"]:
        assert pack_tool in served, f"pack tool {pack_tool!r} is not served"


async def test_commanded_tools_outside_the_pack_are_core():
    """A tool this skill's prose commands but does not pack must be in core — reachable
    with no skill load. Core is DERIVED (served minus every seeded pack), so a later
    skill that packs one of these fails here rather than silently blinding this one."""
    core = (await _served()) - _every_pack()
    for name in _COMMANDED_CORE:
        assert name in core, (
            f"{name!r} is commanded by skill_deep_research.md but is not core — it is "
            "either unserved or packed by another skill, so the model cannot call it"
        )


def test_the_body_actually_names_the_tools_this_test_pins():
    """Guards the pin above from going vacuous: if the prose stops naming a tool, the
    core assertion for it is asserting nothing about this skill."""
    body = _SKILL["body"]
    for name in _COMMANDED_CORE | _EXPECTED_PACK:
        assert name in body, f"skill body no longer names {name!r} — update the pins"
