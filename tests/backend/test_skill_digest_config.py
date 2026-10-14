"""digest-config skill — its examples are the digest parser's grammar, its recipes command core tools."""

import re

import agent_skills
from helpers import skill_frontmatter
from pipeline.digest.run import parse_digest_config

_CONTENT = (agent_skills.CONFIGS_DIR / "skill_digest_config.md").read_text(encoding="utf-8").strip()
_SKILL = skill_frontmatter(_CONTENT)
_BODY = _SKILL["body"]

# Tools the recipes command and the skill does NOT pack — each must be core.
_COMMANDED_CORE = {"read_document", "create_document", "edit_document", "get_project_structure"}


async def _served() -> set[str]:
    from agent.tools import agent_toolset
    from helpers import pinned_tool_gates

    with pinned_tool_gates(sandbox=True):
        return {t["function"]["name"] for t in await agent_toolset()}


def _every_pack() -> set[str]:
    packed: set[str] = set()
    for path in sorted(agent_skills.CONFIGS_DIR.glob("skill_*.md")):
        packed.update(skill_frontmatter(path.read_text(encoding="utf-8"))["tools"])
    return packed


def test_parses_as_a_skill_with_an_empty_pack():
    assert _SKILL["name"] == "digest-config"
    assert _SKILL["tools"] == []


async def test_commanded_tools_are_core_and_named():
    core = (await _served()) - _every_pack()
    for name in _COMMANDED_CORE:
        assert name in _BODY, f"skill body no longer names {name!r} — update the pins"
        assert name in core, f"{name!r} is commanded by skill_digest_config.md but is not core"


def test_every_yaml_example_is_a_valid_digest_config():
    """Each example goes through the parser that reads the user's copy."""
    blocks = re.findall(r"```yaml\n.*?```", _BODY, re.DOTALL)
    assert blocks, "the body must carry a fenced ```yaml example"
    for block in blocks:
        config = parse_digest_config(block + "\nInstruction.", "skill example")
        assert config["theme"] and config["instructions"] == "Instruction."


def test_the_key_table_names_every_key_the_parser_reads():
    """Derived from the parser's own output, so a new key fails here until taught."""
    keys = set(parse_digest_config("```yaml\ntheme: x\n```", "probe")) - {"instructions"}
    for key in keys:
        assert f"| `{key}` |" in _BODY, f"the key table does not teach `{key}`"


def test_shipped_skill_docs_lists_the_skill():
    locations = {d["location"] for d in agent_skills.shipped_skill_docs()}
    assert "shipped:skill_digest_config" in locations
