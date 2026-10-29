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

import re
import textwrap

import agent_skills
from helpers import skill_frontmatter
from pipeline.core.config import (
    _extract_sections,
    _parse_yaml_from_code_block,
    _ranges_tables_from_content,
    _resolve_variable_dicts,
    mark_table_attr_keys,
    validate_mark_tables,
)

_CONTENT = (agent_skills.CONFIGS_DIR / "skill_extractor_config.md").read_text(
    encoding="utf-8"
).strip()
_SKILL = skill_frontmatter(_CONTENT)
_BODY = _SKILL["body"]


def _yaml_blocks(body: str) -> list[str]:
    """Every fenced ```yaml example carried by the skill body."""
    return re.findall(r"```yaml\n(.*?)```", body, re.DOTALL)


def test_bound_ranges_examples_pass_the_setup_gates():
    """Every bound-table example passes the SAME gates SetupNode runs, against the
    variables the body's own examples declare — an example teaching a shape the
    gates reject (a scalar is:, an undeclared by:, a missing catch-all) fails here."""
    vars_section: dict = {}
    calculations: dict = {}
    ranges_blocks: list[dict] = []
    for block in _yaml_blocks(_BODY):
        parsed = _parse_yaml_from_code_block(block)
        if "variables" in parsed or "calculate" in parsed:
            section, calc, _ = _extract_sections(parsed)
            vars_section.update(section)
            calculations.update(calc or {})
        else:
            # The REAL ranges-doc path, fence included — the fenced parse is the
            # one that once unwrapped a lone conditional table.
            rejected: dict[str, str] = {}
            tables = _ranges_tables_from_content(
                f"```yaml\n{textwrap.dedent(block)}```", "skill", rejected,
            )
            assert not rejected, f"a ranges example is rejected: {rejected}"
            assert set(tables) == set(parsed), f"a ranges example lost tables: {set(parsed) - set(tables)}"
            ranges_blocks.append(tables)
    variables, types, enums, kinds, _, _ = _resolve_variable_dicts(vars_section)
    bound = 0
    for tables in ranges_blocks:
        validate_mark_tables(tables, variables, calculations, kinds, enums, types)
        bound += sum(
            1 for name, table in tables.items()
            if name in variables and mark_table_attr_keys(table)
        )
    assert bound >= 3, f"expected the flat, is: and by: bound examples, got {bound}"
