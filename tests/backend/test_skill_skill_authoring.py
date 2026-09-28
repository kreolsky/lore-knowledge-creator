"""skill-authoring shipped skill — the judgement layer over save_skill.

Modeled on test_skill_deep_research.py: the pack and the reachability of every
tool the body commands are both DERIVED (pack from the shipped file, core from
the served toolset minus every seeded pack, the sandbox premise from the
shipped sandbox skill's own frontmatter) — nothing here mirrors a literal.
"""


import agent_skills
from helpers import skill_frontmatter

_CONTENT = (agent_skills.CONFIGS_DIR / "skill_skill_authoring.md").read_text(
    encoding="utf-8",
).strip()
_SKILL = skill_frontmatter(_CONTENT)

# Tools the body instructs the agent to call while this skill is loaded.
_COMMANDED = {"save_skill", "sandbox_bash", "sandbox_fetch_skill", "read_document"}


async def _served() -> set[str]:
    """The served surface with the env gates forced open: CI ships no
    sandbox host, and the contract under test is
    reachability, not the gate."""
    from helpers import pinned_tool_gates

    with pinned_tool_gates(sandbox=True):
        from agent.tools import agent_toolset

        return {t["function"]["name"] for t in await agent_toolset()}


def _every_pack() -> set[str]:
    packed: set[str] = set()
    for path in sorted(agent_skills.CONFIGS_DIR.glob("skill_*.md")):
        packed.update(skill_frontmatter(path.read_text(encoding="utf-8"))["tools"])
    return packed


def _sandbox_pack() -> set[str]:
    """The shipped sandbox skill's pack — the recipe's stated premise for
    commanding sandbox_bash: a script already ran in this chat, so the pack
    is active (packs accumulate per chat)."""
    return set(skill_frontmatter(
        (agent_skills.CONFIGS_DIR / "skill_sandbox.md").read_text(encoding="utf-8")
    )["tools"])


def test_parses_as_a_skill():
    assert _SKILL is not None
    assert _SKILL["name"] == "skill-authoring"


def test_empty_pack():
    """Prose-only: always advertised, activates nothing. save_skill being CORE
    (test_save_skill) is what keeps the tool reachable without this pack."""
    assert _SKILL["tools"] == []


def test_shipped_layer_lists_it():
    locations = {d["location"] for d in agent_skills.shipped_skill_docs()}
    assert "shipped:skill_skill_authoring" in locations


async def test_commanded_tools_are_reachable():
    """save_skill and read_document must be CORE (reachable with no skill
    load — core is DERIVED as served minus every seeded pack, so a later skill
    that packs one fails here rather than silently blinding this one).
    sandbox_bash is reached through the sandbox pack the recipe says is
    already active in any chat where its Shape B applies."""
    core = (await _served()) - _every_pack()
    assert "save_skill" in core
    assert "read_document" in core
    assert "sandbox_bash" in (core | _sandbox_pack())
    assert "sandbox_fetch_skill" in (core | _sandbox_pack())


def test_body_carries_the_fixed_conventions():
    """The things the recipe must keep verbatim: the script's ONE home
    (`files:` → `scripts/run.py`, materialized by `sandbox_fetch_skill`), no
    per-user `~/skills/` path any more, and the four template sections the
    saved body copies."""
    body = _SKILL["body"]
    assert "files:" in body
    assert "scripts/run.py" in body
    assert "sandbox_fetch_skill" in body
    assert "~/skills/" not in body
    for section in ("## When", "## Inputs", "## Steps", "## Output"):
        assert section in body, f"template section {section!r} missing"


def test_the_body_actually_names_the_tools_this_test_pins():
    """Guards the pin above from going vacuous: if the prose stops naming a
    tool, the reachability assertion for it is asserting nothing."""
    body = _SKILL["body"]
    for name in _COMMANDED:
        assert name in body, f"skill body no longer names {name!r}"
