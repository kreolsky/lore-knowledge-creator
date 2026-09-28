"""extractor-config skill — the grammar its body teaches is the parser's grammar.

The body teaches the agent to AUTHOR extractor variables/template YAML from chat. Its
examples are therefore CONTRACT, not illustration: every fenced ```yaml block must parse
through _parse_yaml_from_code_block and normalize through _normalize_variable_entry —
the same code that will read the user's copy; every type word must be a real variable
type and every real type must be named (grammar drift fails in BOTH directions, derived
from the constants); every builtin placeholder the "minus the builtins" step must skip
must be listed (derived from render_template's own docstring, not a literal). The pack
is empty, so every tool the recipes command must be core — served minus every seeded
pack, the same derivation as test_skill_deep_research.
"""

import inspect
import re

import agent_skills
from agent.tools import agent_toolset
from helpers import skill_frontmatter
from pipeline.core.config import (
    _extract_sections,
    _normalize_ranges_table,
    _normalize_variable_entry,
    _parse_yaml_from_code_block,
)
from pipeline.core.constants import ALLOWED_VARIABLE_TYPES, CATEGORICAL_VARIABLE_TYPES
from pipeline.extractor.utils import render_template

_CONTENT = (agent_skills.CONFIGS_DIR / "skill_extractor_config.md").read_text(
    encoding="utf-8"
).strip()
_SKILL = skill_frontmatter(_CONTENT)
_BODY = _SKILL["body"]

# Tools the recipes command and the skill does NOT pack — each must be core.
_COMMANDED_CORE = {
    "read_document",
    "create_document",
    "edit_document",
    "get_project_structure",
}

_ALL_TYPES = ALLOWED_VARIABLE_TYPES | CATEGORICAL_VARIABLE_TYPES


async def _served() -> set[str]:
    """The served surface with the env gates forced open: CI ships no
    sandbox host, and the contract under test is pack↔core consistency, not the gate."""
    from helpers import pinned_tool_gates

    with pinned_tool_gates(sandbox=True):
        return {t["function"]["name"] for t in await agent_toolset()}


def _every_pack() -> set[str]:
    packed: set[str] = set()
    for path in sorted(agent_skills.CONFIGS_DIR.glob("skill_*.md")):
        packed.update(skill_frontmatter(path.read_text(encoding="utf-8"))["tools"])
    return packed


def _yaml_blocks(body: str) -> list[str]:
    """Every fenced ```yaml example carried by the skill body."""
    return re.findall(r"```yaml\n(.*?)```", body, re.DOTALL)


def test_parses_as_a_skill_with_an_empty_pack():
    assert _SKILL is not None, "extractor-config must parse as a valid skill"
    assert _SKILL["name"] == "extractor-config"
    assert _SKILL["tools"] == [], "the pack must be empty — body commands core tools only"


async def test_commanded_tools_outside_the_pack_are_core():
    """A tool the recipes command must be reachable with no skill load. Core is DERIVED
    (served minus every seeded pack), so a later skill that packs one of these fails
    here rather than silently blinding this one."""
    core = (await _served()) - _every_pack()
    for name in _COMMANDED_CORE:
        assert name in core, (
            f"{name!r} is commanded by skill_extractor_config.md but is not core — it "
            "is either unserved or packed by another skill, so the model cannot call it"
        )


def test_the_body_actually_names_the_tools_this_test_pins():
    """Guards the pin above from going vacuous: if the prose stops naming a tool, the
    core assertion for it is asserting nothing about this skill."""
    for name in _COMMANDED_CORE:
        assert name in _BODY, f"skill body no longer names {name!r} — update the pins"


def test_every_fenced_yaml_example_is_valid_config():
    """Each example goes through the parser that will read the user's copy: the fence
    survives _parse_yaml_from_code_block, and the block validates as the doc kind it
    teaches — a variables/calculate example normalizes entry by entry, a `ranges`
    table example normalizes table by table through resolve_ranges_doc's validator."""
    blocks = _yaml_blocks(_BODY)
    assert blocks, "the body must carry fenced ```yaml examples"
    for block in blocks:
        parsed = _parse_yaml_from_code_block(block)
        if "variables" in parsed or "calculate" in parsed:
            vars_section, _, _ = _extract_sections(parsed)
            assert isinstance(vars_section, dict)
            for name, value in vars_section.items():
                _normalize_variable_entry(name, value)  # must not raise
        else:
            assert parsed, "a ranges example must not be empty"
            for name, table in parsed.items():
                assert isinstance(name, str) and name.isidentifier(), (
                    f"ranges example table {name!r} must be an identifier — "
                    "range() parses table names as code"
                )
                assert _normalize_ranges_table(table) is not None, (
                    f"ranges example table {name!r} is not a valid interval table"
                )


def test_shared_defaults_example_two_from_anchor_one_override():
    """The merge-key example: two fields inherit the anchor's default, one field's own
    default wins, and the top-level anchor never becomes a field."""
    merged = [b for b in _yaml_blocks(_BODY) if "<<" in b]
    assert len(merged) == 1, "exactly one example must demonstrate merge keys"
    parsed = _parse_yaml_from_code_block(merged[0])
    vars_section, _, _ = _extract_sections(parsed)
    assert set(vars_section) == {"size", "shape", "conclusion"}, (
        "the anchor key must stay top-level and never surface as a field"
    )
    defaults = {}
    for name, value in vars_section.items():
        _, _, _, _, default = _normalize_variable_entry(name, value)
        if default is not None:
            defaults[name] = default
    assert defaults["size"] == "не указано"
    assert defaults["shape"] == "не указано"
    assert defaults["conclusion"] == "нет данных", (
        "the example must also show a per-field override beating the anchor"
    )


def test_type_words_are_exactly_the_real_types_both_ways():
    """Grammar drift, both directions: the body may not name a type the code rejects,
    and may not leave a real type unnamed — both derived from the constants."""
    named = set(re.findall(r"type:\s*([a-z]+)", _BODY))
    assert not (named - _ALL_TYPES), f"body names unknown types: {sorted(named - _ALL_TYPES)}"
    assert not (_ALL_TYPES - named), f"body forgot real types: {sorted(_ALL_TYPES - named)}"


def test_every_render_template_builtin_is_listed_in_the_body():
    """Recipe 1's 'drop the builtins' step is only as good as the builtin list in the
    body — derive the list from render_template's own docstring, not a literal."""
    builtins = set(re.findall(r"\{\{(\w+)\}\}", inspect.getdoc(render_template)))
    assert builtins, "docstring shape changed — derive the builtin list anew"
    for name in sorted(builtins):
        assert f"{{{{{name}}}}}" in _BODY, (
            f"builtin {{{{{name}}}}} missing from the body — the 'minus the builtins' "
            "step would strip a real field or keep a builtin as one"
        )


def test_shipped_skill_docs_lists_the_skill():
    locations = {d["location"] for d in agent_skills.shipped_skill_docs()}
    assert "shipped:skill_extractor_config" in locations
